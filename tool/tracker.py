"""
Run Dispersy in standalone tracker mode.

Outputs statistics every 300 seconds:
- BANDWIDTH BYTES-UP BYTES-DOWN
- COMMUNITY COUNT(OVERLAYS) COUNT(KILLED-OVERLAYS)
- CANDIDATE COUNT(ALL_CANDIDATES)                       18/07/13 no longer used
- CANDIDATE2 COUNT(VERIFIED_CANDIDATES)                 18/07/13 replaces CANDIDATE

Outputs active peers whenever encountered:
- REQ_IN2 HEX(COMMUNITY) hex(MEMBER) DISPERSY-VERSION OVERLAY-VERSION ADDRESS PORT
- RES_IN2 HEX(COMMUNITY) hex(MEMBER) DISPERSY-VERSION OVERLAY-VERSION ADDRESS PORT

Outputs destroyed communities whenever encountered:
- DESTROY_IN HEX(COMMUNITY) hex(MEMBER) DISPERSY-VERSION OVERLAY-VERSION ADDRESS PORT
- DESTROY_OUT HEX(COMMUNITY) hex(MEMBER) DISPERSY-VERSION OVERLAY-VERSION ADDRESS PORT

Note that there is no output for REQ_IN2 for destroyed overlays.  Instead a DESTROY_OUT is given
whenever a introduction request is received for a destroyed overlay.
"""

if __name__ == "__main__":
    # Concerning the relative imports, from PEP 328:
    # http://www.python.org/dev/peps/pep-0328/
    #
    #    Relative imports use a module's __name__ attribute to determine that module's position in
    #    the package hierarchy. If the module's name does not contain any package information
    #    (e.g. it is set to '__main__') then relative imports are resolved as if the module were a
    #    top level module, regardless of where the module is actually located on the file system.
    print "Usage: python -c \"from dispersy.tool.tracker import main; main()\" [--statedir DIR] [--ip ADDR] [--port PORT] [--crypto TYPE]"
    exit(1)


from time import time
import errno
import logging.config
# optparse is deprecated since python 2.7
import optparse
import os
import signal
import sys

# use logger.conf if it exists
if os.path.exists("logger.conf"):
    # will raise an exception when logger.conf is malformed
    logging.config.fileConfig("logger.conf")
# fallback to basic configuration when needed
logging.basicConfig(format="%(asctime)-15s [%(levelname)s] %(message)s")

from ..candidate import BootstrapCandidate, LoopbackCandidate
from ..community import Community, HardKilledCommunity
from ..conversion import BinaryConversion
from ..crypto import NoVerifyCrypto, NoCrypto
from ..dispersy import Dispersy
from ..endpoint import StandaloneEndpoint
from ..exception import ConversionNotFoundException, CommunityNotFoundException
from ..logger import get_logger, get_context_filter
from ..message import Message, DropMessage
from .mainthreadcallback import MainThreadCallback
logger = get_logger(__name__)

if sys.platform == 'win32':
    SOCKET_BLOCK_ERRORCODE = 10035  # WSAEWOULDBLOCK
else:
    SOCKET_BLOCK_ERRORCODE = errno.EWOULDBLOCK


class TrackerHardKilledCommunity(HardKilledCommunity):

    def __init__(self, *args, **kargs):
        super(TrackerHardKilledCommunity, self).__init__(*args, **kargs)
        # communities are cleaned based on a 'strike' rule.  periodically, we will check is there
        # are active candidates, when there are 'strike' is set to zero, otherwise it is incremented
        # by one.  once 'strike' reaches a predefined value the community is cleaned
        self._strikes = 0

    def update_strikes(self, now):
        # does the community have any active candidates
        self._strikes += 1
        return self._strikes

    def dispersy_on_introduction_request(self, messages):
        hex_cid = messages[0].community.cid.encode("HEX")
        for message in messages:
            host, port = message.candidate.sock_addr
            print "DESTROY_OUT", hex_cid, message.authentication.member.mid.encode("HEX"), ord(message.conversion.dispersy_version), ord(message.conversion.community_version), host, port
        return super(TrackerHardKilledCommunity, self).dispersy_on_introduction_request(messages)


class TrackerCommunity(Community):

    """
    This community will only use dispersy-candidate-request and dispersy-candidate-response messages.
    """
    def __init__(self, *args, **kargs):
        super(TrackerCommunity, self).__init__(*args, **kargs)
        # communities are cleaned based on a 'strike' rule.  periodically, we will check is there
        # are active candidates, when there are 'strike' is set to zero, otherwise it is incremented
        # by one.  once 'strike' reaches a predefined value the community is cleaned
        self._strikes = 0

        self._walked_stumbled_candidates = self._iter_categories([u'walk', u'stumble'])

    def initiate_meta_messages(self):
        messages = super(TrackerCommunity, self).initiate_meta_messages()

        # remove all messages that we should not be using
        tracker_messages = [u"dispersy-introduction-request",
                     u"dispersy-introduction-response",
                     u"dispersy-puncture-request",
                     u"dispersy-puncture",
                     u"dispersy-identity",
                     u"dispersy-missing-identity",

                     u"dispersy-authorize",
                     u"dispersy-revoke",
                     u"dispersy-missing-proof",
                     u"dispersy-destroy-community"]

        messages = [message for message in messages if message.name in tracker_messages]
        return messages

    @property
    def dispersy_auto_download_master_member(self):
        return False

    @property
    def dispersy_sync_bloom_filter_strategy(self):
        # disable sync bloom filter
        return lambda request_cache: None

    @property
    def dispersy_acceptable_global_time_range(self):
        # we will accept the full 64 bit global time range
        return 2 ** 64 - self._global_time

    def update_strikes(self, now):
        # does the community have any active candidates
        if any(self.dispersy_yield_verified_candidates()):
            self._strikes = 0
        else:
            self._strikes += 1
        return self._strikes

    def initiate_conversions(self):
        return [BinaryConversion(self, "\x00")]

    def get_conversion_for_packet(self, packet):
        try:
            return super(TrackerCommunity, self).get_conversion_for_packet(packet)

        except ConversionNotFoundException:
            # the dispersy version MUST BE available.  Currently we only support \x00: BinaryConversion
            if packet[0] == "\x00":
                self.add_conversion(BinaryConversion(self, packet[1]))

            # try again
            return super(TrackerCommunity, self).get_conversion_for_packet(packet)

    def dispersy_cleanup_community(self, message):
        # since the trackers use in-memory databases, we need to store the destroy-community
        # message, and all associated proof, separately.
        host, port = message.candidate.sock_addr
        print "DESTROY_IN", self._cid.encode("HEX"), message.authentication.member.mid.encode("HEX"), ord(message.conversion.dispersy_version), ord(message.conversion.community_version), host, port

        write = open(self._dispersy.persistent_storage_filename, "a+").write
        write("# received dispersy-destroy-community from %s\n" % (str(message.candidate),))

        identity_id = self._meta_messages[u"dispersy-identity"].database_id
        execute = self._dispersy.database.execute
        messages = [message]
        stored = set()
        while messages:
            message = messages.pop()

            if not message.packet in stored:
                stored.add(message.packet)
                write(" ".join((message.name, message.packet.encode("HEX"), "\n")))

                if not message.authentication.member.public_key in stored:
                    try:
                        packet, = execute(u"SELECT packet FROM sync WHERE meta_message = ? AND member = ?", (identity_id, message.authentication.member.database_id)).next()
                    except StopIteration:
                        pass
                    else:
                        write(" ".join(("dispersy-identity", str(packet).encode("HEX"), "\n")))

                _, proofs = self._timeline.check(message)
                messages.extend(proofs)

        return TrackerHardKilledCommunity

    def dispersy_get_introduce_candidate(self, exclude_candidate=None):
        """
        Get an active candidate that is part of this community in Round Robin (Not random anymore).
        """
        assert all(not sock_address in self._candidates for sock_address in self._dispersy._bootstrap_candidates.iterkeys()), "none of the bootstrap candidates may be in self._candidates"
        first_candidate = None
        while True:
            result = self._walked_stumbled_candidates.next()
            if result == first_candidate:
                result = None

            if not first_candidate:
                first_candidate = result

            if result and exclude_candidate:
                # same candidate as requesting the introduction
                if result == exclude_candidate:
                    continue

                # cannot introduce a non-tunnelled candidate to a tunneled candidate (it's swift instance will not
                # get it)
                if not exclude_candidate.tunnel and result.tunnel:
                    continue

                # cannot introduce two nodes that are behind a different symmetric NAT
                if (exclude_candidate.connection_type == u"symmetric-NAT" and
                    result.connection_type == u"symmetric-NAT" and
                    not exclude_candidate.wan_address[0] == result.wan_address[0]):
                    continue

            return result

class TrackerDispersy(Dispersy):

    def __init__(self, callback, endpoint, working_directory, silent=False, crypto=NoVerifyCrypto()):
        super(TrackerDispersy, self).__init__(callback, endpoint, working_directory, u":memory:", crypto)

        # location of persistent storage
        self._persistent_storage_filename = os.path.join(working_directory, "persistent-storage.data")
        self._silent = silent
        self._my_member = None

    def start(self, timeout=10.0):
        if super(TrackerDispersy, self).start(timeout):
            self._create_my_member()
            self._load_persistent_storage()
            self._callback.register(self._unload_communities)

            self.define_auto_load(TrackerCommunity, self._my_member)
            self.define_auto_load(TrackerHardKilledCommunity, self._my_member)

            if not self._silent:
                self._callback.register(self._report_statistics)

            return True
        return False

    def _create_my_member(self):
        # generate a new my-member
        ec = self.crypto.generate_key(u"very-low")
        self._my_member = self.get_member(private_key=self.crypto.key_to_bin(ec))

    @property
    def persistent_storage_filename(self):
        return self._persistent_storage_filename

    def get_community(self, cid, load=False, auto_load=True):
        try:
            return super(TrackerDispersy, self).get_community(cid, True, True)
        except CommunityNotFoundException:
            self._communities[cid] = TrackerCommunity(self, self.get_temporary_member_from_id(cid), self._my_member)
            return self._communities[cid]

    def _load_persistent_storage(self):
        # load all destroyed communities
        try:
            packets = [packet.decode("HEX") for _, packet in (line.split() for line in open(self._persistent_storage_filename, "r") if not line.startswith("#"))]
        except IOError:
            pass
        else:
            candidate = LoopbackCandidate()
            for packet in reversed(packets):
                try:
                    self.on_incoming_packets([(candidate, packet)], cache=False, timestamp=time())
                except:
                    logger.exception("Error while loading from persistent-destroy-community.data")

    def _unload_communities(self):
        def is_active(community, now):
            # check 1: does the community have any active candidates
            if community.update_strikes(now) < 3:
                return True

            # check 2: does the community have any cached messages waiting to be processed
            for meta in self._batch_cache.iterkeys():
                if meta.community == community:
                    return True

            # the community is inactive
            return False

        while True:
            yield 180.0
            now = time()
            inactive = [community for community in self._communities.itervalues() if not is_active(community, now)]
            logger.debug("cleaning %d/%d communities", len(inactive), len(self._communities))
            for community in inactive:
                community.unload_community()

    def _report_statistics(self):
        while True:
            yield 300.0
            mapping = {TrackerCommunity: 0, TrackerHardKilledCommunity: 0}
            for community in self._communities.itervalues():
                mapping[type(community)] += 1

            print "BANDWIDTH", self._endpoint.total_up, self._endpoint.total_down
            print "COMMUNITY", mapping[TrackerCommunity], mapping[TrackerHardKilledCommunity]
            print "CANDIDATE2", sum(len(list(community.dispersy_yield_verified_candidates())) for community in self._communities.itervalues())

            if self._statistics.outgoing:
                for key, value in self._statistics.outgoing.iteritems():
                    print "OUTGOING", key, value

    def create_introduction_request(self, community, destination, allow_sync, forward=True):
        return

    def on_introduction_request(self, messages):
        if not self._silent:
            hex_cid = self.cid.encode("HEX")
            for message in messages:
                host, port = message.candidate.sock_addr
                print "REQ_IN2", hex_cid, message.authentication.member.mid.encode("HEX"), ord(message.conversion.dispersy_version), ord(message.conversion.community_version), host, port
        return super(TrackerDispersy, self).on_introduction_request(messages)

    def on_introduction_response(self, messages):
        if not self._silent:
            hex_cid = self.cid.encode("HEX")
            for message in messages:
                host, port = message.candidate.sock_addr
                print "RES_IN2", hex_cid, message.authentication.member.mid.encode("HEX"), ord(message.conversion.dispersy_version), ord(message.conversion.community_version), host, port
        return super(TrackerDispersy, self).on_introduction_response(messages)


def main():
    command_line_parser = optparse.OptionParser()
    command_line_parser.add_option("--profiler", action="store_true", help="use cProfile on the Dispersy thread", default=False)
    command_line_parser.add_option("--memory-dump", action="store_true", help="use meliae to dump the memory periodically", default=False)
    command_line_parser.add_option("--statedir", action="store", type="string", help="Use an alternate statedir", default=".")
    command_line_parser.add_option("--ip", action="store", type="string", default="0.0.0.0", help="Dispersy uses this ip")
    command_line_parser.add_option("--port", action="store", type="int", help="Dispersy uses this UDL port", default=6421)
    command_line_parser.add_option("--silent", action="store_true", help="Prevent tracker printing to console", default=False)
    command_line_parser.add_option("--crypto", action="store", type="string", default="ECCrytpo", help="The Crypto object type Dispersy is going to use")

    context_filter = get_context_filter()
    command_line_parser.add_option("--log-identifier", type="string", help="this 'identifier' key is included in each log entry (i.e. it can be used in the logger format string)", default=context_filter.identifier)

    # parse command-line arguments
    opt, _ = command_line_parser.parse_args()

    # set the log identifier
    context_filter.identifier = opt.log_identifier

    # crypto
    if opt.crypto == 'NoCrypto':
        crypto = NoCrypto()
    else:
        crypto = NoVerifyCrypto()

    # setup
    dispersy = TrackerDispersy(MainThreadCallback("Dispersy"), StandaloneEndpoint(opt.port, opt.ip), unicode(opt.statedir), bool(opt.silent), crypto)

    def signal_handler(sig, frame):
        logger.warning("Received signal '%s' in %s (shutting down)", sig, frame)
        dispersy.stop()
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # start
    if not dispersy.start():
        raise RuntimeError("Unable to start Dispersy")

    # wait forever
    dispersy.callback.loop()

    # return 1 on exception, otherwise 0
    exit(1 if dispersy.callback.exception else 0)
