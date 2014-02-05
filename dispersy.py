"""
The Distributed Permission System, or Dispersy, is a platform to simplify the design of distributed
communities.  At the heart of Dispersy lies a simple identity and message handling system where each
community and each user is uniquely and securely identified using elliptic curve cryptography.

Since we can not guarantee each member to be online all the time, messages that they created at one
point in time should be able to retain their meaning even when the member is off-line.  This can be
achieved by signing such messages and having them propagated though other nodes in the network.
Unfortunately, this increases the strain on these other nodes, which we try to alleviate using
specific message policies, which will be described below.

Following from this, we can easily package each message into one UDP packet to simplify
connect-ability problems since UDP packets are much easier to pass though NAT's and firewalls.

Earlier we hinted that messages can have different policies.  A message has the following four
different policies, and each policy defines how a specific part of the message should be handled.

 - Authentication defines if the message is signed, and if so, by how many members.

 - Resolution defines how the permission system should resolve conflicts between messages.

 - Distribution defines if the message is send once or if it should be gossiped around.  In the
   latter case, it can also define how many messages should be kept in the network.

 - Destination defines to whom the message should be send or gossiped.

To ensure that every node handles a messages in the same way, i.e. has the same policies associated
to each message, a message exists in two stages.  The meta-message and the implemented-message
stage.  Each message has one meta-message associated to it and tells us how the message is supposed
to be handled.  When a message is send or received an implementation is made from the meta-message
that contains information specifically for that message.  For example: a meta-message could have the
member-authentication-policy that tells us that the message must be signed by a member but only the
an implemented-message will have data and this signature.

A community can tweak the policies and how they behave by changing the parameters that the policies
supply.  Aside from the four policies, each meta-message also defines the community that it is part
of, the name it uses as an internal identifier, and the class that will contain the payload.
"""
import logging
import os
import sys
from collections import defaultdict
from itertools import groupby, count
from pprint import pformat
from socket import inet_aton, error as socket_error
from struct import unpack_from
from time import time
from traceback import print_exc

import netifaces

from .authentication import NoAuthentication, MemberAuthentication, DoubleMemberAuthentication
from .bloomfilter import BloomFilter
from .bootstrap import Bootstrap
from .cache import (MissingMemberCache, MissingProofCache, IntroductionRequestCache, MissingSequenceCache,
                    MissingSequenceOverviewCache, SignatureRequestCache, MissingMessageCache)
from .candidate import BootstrapCandidate, LoopbackCandidate, WalkCandidate, Candidate
from .crypto import DispersyCrypto, ECCrypto
from .destination import CommunityDestination, CandidateDestination
from .dispersydatabase import DispersyDatabase
from .distribution import (SyncDistribution, FullSyncDistribution, LastSyncDistribution, DirectDistribution,
                           GlobalTimePruning)
from .logger import get_logger
from .member import DummyMember, Member
from .message import (Packet, Message, DropMessage, DelayMessage, DelayMessageByProof, DelayMessageBySequence,
                      DelayMessageByMissingMessage, DropPacket, DelayPacket)
from .statistics import DispersyStatistics


try:
    # python 2.7 only...
    from collections import OrderedDict
except ImportError:
    from .python27_ordereddict import OrderedDict


logger = get_logger(__name__)

if __debug__:
    from .callback import Callback
    from .endpoint import Endpoint


class Dispersy(object):

    """
    The Dispersy class provides the interface to all Dispersy related commands, managing the in- and
    outgoing data for, possibly, multiple communities.
    """

    def __init__(self, callback, endpoint, working_directory, database_filename=u"dispersy.db", crypto=ECCrypto()):
        """
        Initialise a Dispersy instance.

        @param callback: Instance for callback scheduling.
        @type callback: Callback

        @param endpoint: Instance for communication.
        @type callback: Endpoint

        @param working_directory: The directory where all files should be stored.
        @type working_directory: unicode

        @param database_filename: The database filename or u":memory:"
        @type database_filename: unicode
        """
        assert isinstance(callback, Callback), type(callback)
        assert isinstance(endpoint, Endpoint), type(endpoint)
        assert isinstance(working_directory, unicode), type(working_directory)
        assert isinstance(database_filename, unicode), type(database_filename)
        assert isinstance(crypto, DispersyCrypto), type(crypto)
        super(Dispersy, self).__init__()

        # the thread we will be using
        self._callback = callback

        # communication endpoint
        self._endpoint = endpoint

        # where we store all data
        self._working_directory = os.path.abspath(working_directory)

        # _pending_callbacks contains all id's for registered calls that should be removed when the
        # Dispersy is stopped.  most of the time this contains all the generators that are used
        self._pending_callbacks = {}
        # add id(self) into the callback identifier to ensure multiple Dispersy instances can use
        # the same Callback instance
        self._pending_callbacks[u"candidate-walker"] = u"dispersy-candidate-walker-%d" % (id(self),)

        self._member_cache_by_public_key = OrderedDict()
        self._member_cache_by_hash = dict()
        self._member_cache_by_database_id = dict()

        # our data storage
        if not database_filename == u":memory:":
            database_directory = os.path.join(self._working_directory, u"sqlite")
            if not os.path.isdir(database_directory):
                os.makedirs(database_directory)
            database_filename = os.path.join(database_directory, database_filename)
        self._database = DispersyDatabase(database_filename)

        self._crypto = crypto

        # indicates what our connection type is.  currently it can be u"unknown", u"public", or
        # u"symmetric-NAT"
        self._connection_type = u"unknown"

        # our LAN and WAN addresses
        self._local_interfaces = list(self._get_interface_addresses())
        interface = self._guess_lan_address(self._local_interfaces)
        self._lan_address = ((interface.address if interface else "0.0.0.0"), 0)
        self._wan_address = ("0.0.0.0", 0)
        self._wan_address_votes = defaultdict(set)
        logger.debug("my LAN address is %s:%d", self._lan_address[0], self._lan_address[1])
        logger.debug("my WAN address is %s:%d", self._wan_address[0], self._wan_address[1])
        logger.debug("my connection type is %s", self._connection_type)

        # bootstrap peers
        self._bootstrap_candidates = dict()

        # communities that can be auto loaded.  classification:(cls, args, kargs) pairs.
        self._auto_load_communities = OrderedDict()

        # loaded communities.  cid:Community pairs.
        self._communities = {}
        self._walker_commmunities = []

        self._check_distribution_batch_map = {DirectDistribution: self._check_direct_distribution_batch,
                                              FullSyncDistribution: self._check_full_sync_distribution_batch,
                                              LastSyncDistribution: self._check_last_sync_distribution_batch}

        # progress handlers (used to notify the user when something will take a long time)
        self._progress_handlers = []

        # statistics...
        self._statistics = DispersyStatistics(self)

        # memory profiler
        if "--memory-dump" in sys.argv:
            def memory_dump():
                from meliae import scanner
                start = time()
                try:
                    while True:
                        yield float(60 * 60)
                        scanner.dump_all_objects("memory-%d.out" % (time() - start))
                except GeneratorExit:
                    scanner.dump_all_objects("memory-%d-shutdown.out" % (time() - start))

            self._callback.register(memory_dump)

    @staticmethod
    def _get_interface_addresses():
        """
        Yields Interface instances for each available AF_INET interface found.

        An Interface instance has the following properties:
        - name          (i.e. "eth0")
        - address       (i.e. "10.148.3.254")
        - netmask       (i.e. "255.255.255.0")
        - broadcast     (i.e. "10.148.3.255")
        """
        class Interface(object):

            def __init__(self, name, address, netmask, broadcast):
                self.name = name
                self.address = address
                self.netmask = netmask
                self.broadcast = broadcast
                self._l_address, = unpack_from(">L", inet_aton(address))
                self._l_netmask, = unpack_from(">L", inet_aton(netmask))

            def __contains__(self, address):
                assert isinstance(address, str), type(address)
                l_address, = unpack_from(">L", inet_aton(address))
                return (l_address & self._l_netmask) == (self._l_address & self._l_netmask)

            def __str__(self):
                return "<{self.__class__.__name__} \"{self.name}\" addr:{self.address} mask:{self.netmask}>".format(self=self)

            def __repr__(self):
                return "<{self.__class__.__name__} \"{self.name}\" addr:{self.address} mask:{self.netmask}>".format(self=self)

        for interface in netifaces.interfaces():
            try:
                addresses = netifaces.ifaddresses(interface)

            except ValueError:
                # some interfaces are given that are invalid, we encountered one called ppp0
                pass

            else:
                for option in addresses.get(netifaces.AF_INET, []):
                    try:
                        yield Interface(interface, option.get("addr"), option.get("netmask"), option.get("broadcast"))

                    except TypeError:
                        # some interfaces have no netmask configured, causing a TypeError when
                        # trying to unpack _l_netmask
                        pass

    @staticmethod
    def _guess_lan_address(interfaces, default=None):
        """
        Chooses the most likely Interface instance out of INTERFACES to use as our LAN address.

        INTERFACES can be obtained from _get_interface_addresses()
        DEFAULT is used when no appropriate Interface can be found
        """
        assert isinstance(interfaces, list), type(interfaces)
        blacklist = ["127.0.0.1", "0.0.0.0", "255.255.255.255"]

        # prefer interfaces where we have a broadcast address
        for interface in interfaces:
            if interface.broadcast and interface.address and not interface.address in blacklist:
                logger.debug("%s", interface)
                return interface

        # Exception for virtual machines/containers
        for interface in interfaces:
            if interface.address and not interface.address in blacklist:
                logger.debug("%s", interface)
                return interface

        logger.error("Unable to find our public interface!")
        return default

    def _resolve_bootstrap_candidates(self, timeout):
        """
        Resolve all bootstrap candidates within TIMEOUT seconds or fail.

        When TIMEOUT is larger than 0.0 we first attempts to resolve the bootstrap candidates while
        blocking for at most TIMEOUT seconds, returning True when successful.

        When TIMEOUT is 0.0 or when the bootstrap candidates were not all resolved within TIMEOUT
        seconds we will return False and schedule a retry every 300 seconds until all bootstrap
        candidates are successfully resolved.

        @param timeout: Number of maximum seconds to wait.
        @type timeout: float

        @return: True when all bootstrap candidates are resolved, otherwise False.
        @rtype: boolean
        """
        assert self._callback.is_current_thread
        assert isinstance(timeout, float), type(timeout)
        assert timeout >= 0.0, timeout

        def on_results(success):
            assert self._callback.is_current_thread
            assert isinstance(success, bool), type(success)

            # even when success is False it is still possible that *some* addresses were resolved
            previous_length = len(self._bootstrap_candidates)
            for candidate in bootstrap.candidates:
                # we do not want existing candidates to be overwritten, hence we can not use
                # _bootstrap_candidates.update
                if not candidate.sock_addr in self._bootstrap_candidates:
                    self._bootstrap_candidates[candidate.sock_addr] = candidate
            logger.debug("there are %d available bootstrap candidates (%d new)",
                         len(self._bootstrap_candidates), len(self._bootstrap_candidates) - previous_length)

            # ensure none of the current candidates in Community._candidates point to bootstrap
            # candidates
            for community in self._communities.itervalues():
                community.update_bootstrap_candidates(self._bootstrap_candidates.itervalues())

            if success:
                logger.debug("resolved all bootstrap addresses")

        def retry_until_success():
            for counter in count(1):
                logger.warning("resolving bootstrap addresses (attempt #%d)", counter)
                bootstrap.resolve(on_results)

                # delay should be larger than the timeout used for bootstrap.resolve()
                yield 300.0

                if bootstrap.are_resolved:
                    break

        alternate_addresses = Bootstrap.load_addresses_from_file(os.path.join(self._working_directory, "bootstraptribler.txt"))
        default_addresses = Bootstrap.get_default_addresses()
        bootstrap = Bootstrap(self._callback, alternate_addresses or default_addresses)

        if timeout == 0.0:
            # retry until successful
            self._callback.register(retry_until_success)

        else:
            # first attempt will block for at most TIMEOUT seconds
            logger.debug("resolving bootstrap addresses (%.1s timeout)", timeout)
            # give low priority to ensure that on_results is called before the call returns
            self._callback.call(bootstrap.resolve, kargs=dict(func=on_results, timeout=timeout, blocking=True), priority=-128)

            if not bootstrap.are_resolved:
                # unable to resolve all... retry until successful
                self._callback.register(retry_until_success, delay=300.0)

        return bootstrap.are_resolved

    @property
    def working_directory(self):
        """
        The full directory path where all dispersy related files are stored.
        @rtype: unicode
        """
        return self._working_directory

    @property
    def endpoint(self):
        """
        The endpoint object used to send packets.
        @rtype: Object with a send(address, data) method
        """
        return self._endpoint

    def _endpoint_ready(self):
        """
        Guess our LAN and WAN address from information provided by endpoint.

        This method is called immediately after endpoint.start finishes.
        """
        host, port = self._endpoint.get_address()
        logger.info("update LAN address %s:%d -> %s:%d", self._lan_address[0], self._lan_address[1], self._lan_address[0], port)
        self._lan_address = (self._lan_address[0], port)

        # at this point we do not yet have a WAN address, set it to the LAN address to ensure we
        # have something
        assert self._wan_address == ("0.0.0.0", 0)
        logger.info("update WAN address %s:%d -> %s:%d", self._wan_address[0], self._wan_address[1], self._lan_address[0], self._lan_address[1])
        self._wan_address = self._lan_address

        if not self.is_valid_address(self._lan_address):
            logger.info("update LAN address %s:%d -> %s:%d", self._lan_address[0], self._lan_address[1], host, self._lan_address[1])
            self._lan_address = (host, self._lan_address[1])

            if not self.is_valid_address(self._lan_address):
                logger.info("update LAN address %s:%d -> %s:%d", self._lan_address[0], self._lan_address[1], self._wan_address[0], self._lan_address[1])
                self._lan_address = (self._wan_address[0], self._lan_address[1])

        # our address may not be a bootstrap address
        if self._lan_address in self._bootstrap_candidates:
            del self._bootstrap_candidates[self._lan_address]

        # our address may not be a candidate
        for community in self._communities.itervalues():
            community.candidates.pop(self._lan_address, None)

    @property
    def lan_address(self):
        """
        The LAN address where we believe people who are inside our LAN can find us.

        Our LAN address is determined by the default gateway of our
        system and our port.

        @rtype: (str, int)
        """
        return self._lan_address

    @property
    def wan_address(self):
        """
        The wan address where we believe that we can be found from outside our LAN.

        Our wan address is determined by majority voting.  Each time when we receive a message
        that contains an opinion about our wan address, we take this into account.  The
        address with the most votes wins.

        Votes can be added by calling the wan_address_vote(...) method.

        Usually these votes are received through dispersy-introduction-request and
        dispersy-introduction-response messages.

        @rtype: (str, int)
        """
        return self._wan_address

    @property
    def connection_type(self):
        """
        The connection type that we believe we have.

        Currently the following types are recognized:
        - u'unknown': the default value until the actual type can be recognized.
        - u'public': when the LAN and WAN addresses are determined to be the same.
        - u'symmetric-NAT': when each remote peer reports different external port numbers.

        @rtype: unicode
        """
        return self._connection_type

    @property
    def callback(self):
        return self._callback

    @property
    def database(self):
        """
        The Dispersy database singleton.
        @rtype: DispersyDatabase
        """
        return self._database

    @property
    def crypto(self):
        """
        The Dispersy crypto singleton.
        @rtype: DispersyCrypto
        """
        return self._crypto

    @property
    def statistics(self):
        """
        The Statistics instance.
        """
        return self._statistics

    def define_auto_load(self, community_cls, args=(), kargs=None, load=False):
        """
        Tell Dispersy how to load COMMUNITY is needed.

        COMMUNITY_CLS is the community class that is defined.

        ARGS an KARGS are optional arguments and keyword arguments used when a community is loaded
        using COMMUNITY_CLS.load_community(self, master, *ARGS, **KARGS).

        When LOAD is True all available communities of this type will be immediately loaded.

        Returns a list with loaded communities.
        """
        if __debug__:
            from .community import Community
        assert self._callback.is_current_thread, "Must be called from the callback thread"
        assert issubclass(community_cls, Community), type(community_cls)
        assert isinstance(args, tuple), type(args)
        assert kargs is None or isinstance(kargs, dict), type(kargs)
        assert not community_cls.get_classification() in self._auto_load_communities
        assert isinstance(load, bool), type(load)

        if kargs is None:
            kargs = {}
        self._auto_load_communities[community_cls.get_classification()] = (community_cls, args, kargs)

        communities = []
        if load:
            for master in community_cls.get_master_members(self):
                if not master.mid in self._communities:
                    logger.debug("Loading %s at start", community_cls.get_classification())
                    community = community_cls.load_community(self, master, *args, **kargs)
                    communities.append(community)
                    assert community.master_member.mid == master.mid
                    assert community.master_member.mid in self._communities

        return communities

    def undefine_auto_load(self, community):
        """
        Tell Dispersy to no longer load COMMUNITY.

        COMMUNITY is the community class that is defined.
        """
        if __debug__:
            from .community import Community
        assert issubclass(community, Community)
        assert community.get_classification() in self._auto_load_communities
        del self._auto_load_communities[community.get_classification()]

    def attach_progress_handler(self, func):
        assert callable(func), "handler must be callable"
        self._progress_handlers.append(func)

    def detach_progress_handler(self, func):
        assert callable(func), "handler must be callable"
        assert func in self._progress_handlers, "handler is not attached"
        self._progress_handlers.remove(func)

    def get_progress_handlers(self):
        return self._progress_handlers

    def get_member(self, public_key, private_key=""):
        """
        Returns a Member instance associated with public_key.

        Since we have the public_key, we can create this user when it didn't already exist.  Hence,
        this method always succeeds.

        @param public_key: The public key of the member we want to obtain.
        @type public_key: string

        @return: The Member instance associated with public_key.
        @rtype: Member

        @note: This returns -any- Member, it may not be a member that is part of this community.

        @todo: Since this method returns Members that are not specifically bound to any community,
         this method should be moved to Dispersy
        """
        assert isinstance(public_key, str)
        assert isinstance(private_key, str)
        member = self._member_cache_by_public_key.get(public_key)
        if member:
            if private_key and not member.private_key:
                member.set_private_key(private_key)

        else:
            member = Member(self, public_key, private_key)

            # store in caches
            self._member_cache_by_public_key[public_key] = member
            self._member_cache_by_hash[member.mid] = member
            self._member_cache_by_database_id[member.database_id] = member

            # limit cache length
            if len(self._member_cache_by_public_key) > 1024:
                _, pop = self._member_cache_by_public_key.popitem(False)
                del self._member_cache_by_hash[pop.mid]
                del self._member_cache_by_database_id[pop.database_id]

        return member

    def get_new_member(self, securitylevel=u"medium"):
        """
        Returns a Member instance created from a newly generated public key.
        """
        assert isinstance(securitylevel, unicode), type(securitylevel)
        key = self.crypto.generate_key(securitylevel)
        return self.get_member(self.crypto.key_to_bin(key.pub()), self.crypto.key_to_bin(key))

    def get_temporary_member_from_id(self, mid):
        """
        Returns a temporary Member instance reserving the MID until (hopefully) the public key
        becomes available.

        This method should be used with caution as this will create a real Member without having the
        public key available.  This method is (sometimes) used when joining a community when we only
        have its CID (=MID).

        @param mid: The 20 byte sha1 digest indicating a member.
        @type mid: string

        @return: A (Dummy)Member instance
        @rtype: DummyMember or Member
        """
        assert isinstance(mid, str), type(mid)
        assert len(mid) == 20, len(mid)
        return self._member_cache_by_hash.get(mid) or DummyMember(self, mid)

    def get_members_from_id(self, mid):
        """
        Returns zero or more Member instances associated with mid, where mid is the sha1 digest of a
        member public key.

        As we are using only 20 bytes to represent the actual member public key, this method may
        return multiple possible Member instances.  In this case, other ways must be used to figure
        out the correct Member instance.  For instance: if a signature or encryption is available,
        all Member instances could be used, but only one can succeed in verifying or decrypting.

        Since we may not have the public key associated to MID, this method may return an empty
        list.  In such a case it is sometimes possible to DelayPacketByMissingMember to obtain the
        public key.

        @param mid: The 20 byte sha1 digest indicating a member.
        @type mid: string

        @return: A list containing zero or more Member instances.
        @rtype: [Member]

        @note: This returns -any- Member, it may not be a member that is part of this community.
        """
        assert isinstance(mid, str), type(mid)
        assert len(mid) == 20, len(mid)
        member = self._member_cache_by_hash.get(mid)
        if member:
            return [member]

        else:
            # note that this allows a security attack where someone might obtain a crypographic
            # key that has the same sha1 as the master member, however unlikely.  the only way to
            # prevent this, as far as we know, is to increase the size of the community
            # identifier, for instance by using sha256 instead of sha1.
            return [self.get_member(str(public_key))
                    for public_key,
                    in list(self._database.execute(u"SELECT public_key FROM member WHERE mid = ?", (buffer(mid),)))
                    if public_key]

    def get_member_from_database_id(self, database_id):
        """
        Returns a Member instance associated with DATABASE_ID or None when this row identifier is
        not available.
        """
        assert isinstance(database_id, (int, long)), type(database_id)
        member = self._member_cache_by_database_id.get(database_id)
        if not member:
            try:
                public_key, = next(self._database.execute(u"SELECT public_key FROM member WHERE id = ?", (database_id,)))
            except StopIteration:
                pass
            else:
                member = self.get_member(str(public_key))
        return member

    def attach_community(self, community):
        """
        Add a community to the Dispersy instance.

        Each community must be known to Dispersy, otherwise an incoming message will not be able to
        be passed along to it's associated community.

        In general this method is called from the Community.__init__(...) method.

        @param community: The community that will be added.
        @type community: Community
        """
        if __debug__:
            from .community import Community
        assert isinstance(community, Community)
        logger.debug("%s %s", community.cid.encode("HEX"), community.get_classification())
        assert not community.cid in self._communities
        assert not community in self._walker_commmunities
        self._communities[community.cid] = community
        community.dispersy_check_database()

        if community.dispersy_enable_candidate_walker:
            self._walker_commmunities.insert(0, community)
            # restart walker scheduler
            self._callback.replace_register(self._pending_callbacks[u"candidate-walker"], self._candidate_walker)

        # count the number of times that a community was attached
        self._statistics.dict_inc(self._statistics.attachment, community.cid)

        if __debug__:
            # schedule the sanity check... it also checks that the dispersy-identity is available and
            # when this is a create or join this message is created only after the attach_community
            if "--sanity-check" in sys.argv:
                try:
                    self.sanity_check(community)
                except ValueError:
                    logger.exception("sanity check fail for %s", community)
                    assert False, "One or more exceptions occurred during sanity check"

    def detach_community(self, community):
        """
        Remove an attached community from the Dispersy instance.

        Once a community is detached it will no longer receive incoming messages.  When the
        community is marked as auto_load it will be loaded, using community.load_community(...),
        when a message for this community is received.

        @param community: The community that will be added.
        @type community: Community
        """
        if __debug__:
            from .community import Community
        assert isinstance(community, Community)
        logger.debug("%s %s", community.cid.encode("HEX"), community.get_classification())
        assert community.cid in self._communities
        assert self._communities[community.cid] == community
        assert not community.dispersy_enable_candidate_walker or community in self._walker_commmunities, [community.dispersy_enable_candidate_walker, community in self._walker_commmunities]
        del self._communities[community.cid]

        # stop walker
        if community.dispersy_enable_candidate_walker:
            self._walker_commmunities.remove(community)
            if self._walker_commmunities:
                # restart walker scheduler
                self._callback.replace_register(self._pending_callbacks[u"candidate-walker"], self._candidate_walker)
            else:
                # stop walker scheduler
                self._callback.unregister(self._pending_callbacks[u"candidate-walker"])

        # remove any items that are left in the cache
        community.purge_batch_cache()

    def reclassify_community(self, source, destination):
        """
        Change a community classification.

        Each community has a classification that dictates what source code is handling this
        community.  By default the classification of a community is the unicode name of the class in
        the source code.

        In some cases it may be usefull to change the classification, for instance: if community A
        has a subclass community B, where B has similar but reduced capabilities, we could
        reclassify B to A at some point and keep all messages collected so far while using the
        increased capabilities of community A.

        @param source: The community that will be reclassified.  This must be either a Community
         instance (when the community is loaded) or a Member instance giving the master member (when
         the community is not loaded).
        @type source: Community or Member

        @param destination: The new community classification.  This must be a Community class.
        @type destination: Community class
        """
        if __debug__:
            from .community import Community
        assert isinstance(source, (Community, Member))
        assert issubclass(destination, Community)

        destination_classification = destination.get_classification()

        if isinstance(source, Member):
            logger.debug("reclassify <unknown> -> %s", destination_classification)
            master = source

        else:
            logger.debug("reclassify %s -> %s", source.get_classification(), destination_classification)
            assert source.cid in self._communities
            assert self._communities[source.cid] == source
            master = source.master_member
            source.unload_community()

        self._database.execute(u"UPDATE community SET classification = ? WHERE master = ?",
                               (destination_classification, master.database_id))
        assert self._database.changes == 1

        if destination_classification in self._auto_load_communities:
            cls, args, kargs = self._auto_load_communities[destination_classification]
            assert cls == destination, [cls, destination]
        else:
            args = ()
            kargs = {}

        return destination.load_community(self, master, *args, **kargs)

    def has_community(self, cid):
        """
        Returns True when there is a community CID.
        """
        return cid in self._communities

    def get_community(self, cid, load=False, auto_load=True):
        """
        Returns a community by its community id.

        The community id, or cid, is the binary representation of the public key of the master
        member for the community.

        When the community is available but not currently loaded it will be automatically loaded
        when (a) the load parameter is True or (b) the auto_load parameter is True and the auto_load
        flag for this community is True (this flag is set in the database).

        @param cid: The community identifier.
        @type cid: string, of any size

        @param load: When True, will load the community when available and not yet loaded.
        @type load: bool

        @param auto_load: When True, will load the community when available, the auto_load flag is
         True, and, not yet loaded.
        @type load: bool

        @warning: It is possible, however unlikely, that multiple communities will have the same
         cid.  This is currently not handled.
        """
        assert isinstance(cid, str)
        assert isinstance(load, bool), type(load)
        assert isinstance(auto_load, bool)

        try:
            return self._communities[cid]

        except KeyError:
            if load or auto_load:
                try:
                    # have we joined this community
                    classification, auto_load_flag, master_public_key = self._database.execute(u"SELECT community.classification, community.auto_load, member.public_key FROM community JOIN member ON member.id = community.master WHERE mid = ?",
                                                                                               (buffer(cid),)).next()

                except StopIteration:
                    pass

                else:
                    if load or (auto_load and auto_load_flag):

                        if classification in self._auto_load_communities:
                            master = self.get_member(str(master_public_key)) if master_public_key else self.get_temporary_member_from_id(cid)
                            cls, args, kargs = self._auto_load_communities[classification]
                            community = cls.load_community(self, master, *args, **kargs)
                            assert master.mid in self._communities
                            return community

                        else:
                            logger.warning("unable to auto load %s is an undefined classification [%s]", cid.encode("HEX"), classification)

                    else:
                        logger.debug("not allowed to load [%s]", classification)

        raise KeyError(cid)

    def get_communities(self):
        """
        Returns a list with all known Community instances.
        """
        return self._communities.values()

    def get_message(self, community, member, global_time):
        """
        Returns a Member.Implementation instance uniquely identified by its community, member, and
        global_time.

        Returns None if this message is not in the local database.
        """
        if __debug__:
            from .community import Community
        assert isinstance(community, Community)
        assert isinstance(member, Member)
        assert isinstance(global_time, (int, long))
        try:
            packet, = self._database.execute(u"SELECT packet FROM sync WHERE community = ? AND member = ? AND global_time = ?",
                                             (community.database_id, member.database_id, global_time)).next()
        except StopIteration:
            return None
        else:
            return self.convert_packet_to_message(str(packet), community)

    def get_last_message(self, community, member, meta):
        if __debug__:
            from .community import Community
        assert isinstance(community, Community)
        assert isinstance(member, Member)
        assert isinstance(meta, Message)
        try:
            packet, = self._database.execute(u"SELECT packet FROM sync WHERE member = ? AND meta_message = ? ORDER BY global_time DESC LIMIT 1",
                                             (member.database_id, meta.database_id)).next()
        except StopIteration:
            return None
        else:
            return self.convert_packet_to_message(str(packet), community)

    def wan_address_unvote(self, voter):
        """
        Removes and returns one vote made by VOTER.
        """
        assert isinstance(voter, Candidate)
        for vote, voters in self._wan_address_votes.iteritems():
            if voter.sock_addr in voters:
                logger.debug("removing vote for %s made by %s", vote, voter)
                voters.remove(voter.sock_addr)
                if len(voters) == 0:
                    del self._wan_address_votes[vote]
                return vote

    def wan_address_vote(self, address, voter):
        """
        Add one vote and possibly re-determine our wan address.

        Our wan address is determined by majority voting.  Each time when we receive a message
        that contains anothers opinion about our wan address, we take this into account.  The
        address with the most votes wins.

        Usually these votes are received through dispersy-candidate-request and
        dispersy-candidate-response messages.

        @param address: The wan address that the voter believes us to have.
        @type address: (str, int)

        @param voter: The voter candidate.
        @type voter: Candidate
        """
        assert isinstance(address, tuple)
        assert len(address) == 2
        assert isinstance(address[0], str)
        assert isinstance(address[1], int)
        assert isinstance(voter, Candidate), type(voter)

        def set_lan_address(address):
            " Set LAN address when ADDRESS is different from self._LAN_ADDRESS. "
            if self._lan_address == address:
                return False
            else:
                logger.info("update LAN address %s:%d -> %s:%d", self._lan_address[0], self._lan_address[1], address[0], address[1])
                self._lan_address = address
                return True

        def set_wan_address(address):
            " Set WAN address when ADDRESS is different from self._WAN_ADDRESS. "
            if self._wan_address == address:
                return False
            else:
                logger.info("update WAN address %s:%d -> %s:%d", self._wan_address[0], self._wan_address[1], address[0], address[1])
                self._wan_address = address
                return True

        def set_connection_type(connection_type):
            " Set connection type when CONNECTION_TYPE is different from self._CONNECTION_TYPE. "
            if self._connection_type == connection_type:
                return False
            else:
                logger.info("update connection type %s -> %s", self._connection_type, connection_type)
                self._connection_type = connection_type
                return True

        # undo previous vote
        self.wan_address_unvote(voter)

        # ensure ADDRESS is valid
        if not self.is_valid_address(address):
            logger.debug("ignore vote for %s from %s (address is invalid)", address, voter.sock_addr)
            return

        # ignore votes from voters that we know are within any of our LAN interfaces.  these voters
        # can not know our WAN address
        if any(voter.sock_addr[0] in interface for interface in self._local_interfaces):
            logger.debug("ignore vote for %s from %s (voter is within our LAN)", address, voter.sock_addr)
            return

        # do vote
        logger.debug("add vote for %s from %s", address, voter.sock_addr)
        self._wan_address_votes[address].add(voter.sock_addr)

        #
        # check self._lan_address and self._wan_address
        #

        # change when new vote count is equal or higher than old address vote count
        if len(self._wan_address_votes[address]) >= len(self._wan_address_votes.get(self._wan_address, ())) and\
                set_wan_address(address):

            # reassessing our LAN address, perhaps we are running on a roaming device
            self._local_interfaces = list(self._get_interface_addresses())
            interface = self._guess_lan_address(self._local_interfaces)
            lan_address = ((interface.address if interface else "0.0.0.0"), self._lan_address[1])
            if not self.is_valid_address(lan_address):
                lan_address = (self._wan_address[0], self._lan_address[1])
            set_lan_address(lan_address)

            # TODO security threat!  we should never remove bootstrap candidates, for they are our
            # safety net our address may not be a bootstrap address
            if self._wan_address in self._bootstrap_candidates:
                del self._bootstrap_candidates[self._wan_address]
            if self._lan_address in self._bootstrap_candidates:
                del self._bootstrap_candidates[self._lan_address]

            # TODO security threat!  we should not remove candidates based on the votes we obtain,
            # this can be easily misused.  leaving this code to prevent a node talking with itself
            #
            # our address may not be a candidate
            for community in self._communities.itervalues():
                community.candidates.pop(self._wan_address, None)
                community.candidates.pop(self._lan_address, None)

                for candidate in [candidate for candidate in community.candidates.itervalues() if candidate.wan_address == self._wan_address]:
                    community.candidates.pop(candidate.sock_addr, None)

        #
        # check self._connection_type
        #

        if len(self._wan_address_votes) == 1 and self._lan_address == self._wan_address:
            # external peers are reporting the same WAN address that happens to be our LAN address
            # as well
            set_connection_type(u"public")

        elif len(self._wan_address_votes) > 1:
            # external peers are reporting multiple WAN addresses (most likely the same IP with
            # different port numbers)
            set_connection_type(u"symmetric-NAT")

        else:
            # it is possible that, for some time after the WAN address changes, we will believe that
            # the connection type is symmetric NAT.  once votes have been pruned we may find that we
            # are no longer behind a symmetric-NAT
            set_connection_type(u"unknown")

    def _is_duplicate_sync_message(self, message):
        """
        Returns True when this message is a duplicate, otherwise the message must be processed.

        === Problem: duplicate message ===
        The simplest reason to reject an incoming message is when we already have it, based on the
        community, member, and global time.  No further action is performed.

        === Problem: duplicate message, but that message is undone ===
        When a message is undone it should no longer be synced.  Hence, someone who syncs an undone
        message must not be aware of the undo message yet.  We will drop this message, but we will
        also send the appropriate undo message as a response.

        === Problem: same payload, different signature ===
        There is a possibility that a message is created that contains exactly the same payload but
        has a different signature.  This can occur when a message is created, forwarded, and for
        some reason the database is reset.  The next time that the client starts the exact same
        message may be generated.  However, because EC signatures contain a random element the
        signature will be different.

        This results in continues transfers because the bloom filters identify the two messages
        as different while the community/member/global_time triplet is the same.

        To solve this, we will silently replace one message with the other.  We choose to keep
        the message with the highest binary value while destroying the one with the lower binary
        value.

        === Optimization: temporarily modify the bloom filter ===
        Note: currently we generate bloom filters on the fly, therefore, we can not use this
        optimization.

        To further optimize, we will add both messages to our bloom filter whenever we detect
        this problem.  This will ensure that we do not needlessly receive the 'invalid' message
        until the bloom filter is synced with the database again.
        """
        community = message.community
        # fetch the duplicate binary packet from the database
        try:
            have_packet, undone = self._database.execute(u"SELECT packet, undone FROM sync WHERE community = ? AND member = ? AND global_time = ?",
                                                        (community.database_id, message.authentication.member.database_id, message.distribution.global_time)).next()
        except StopIteration:
            logger.debug("this message is not a duplicate")
            return False

        else:
            have_packet = str(have_packet)
            if have_packet == message.packet:
                # exact binary duplicate, do NOT process the message
                logger.warning("received identical message %s %d@%d from %s %s",
                               message.name,
                               message.authentication.member.database_id,
                               message.distribution.global_time,
                               message.candidate,
                               "(this message is undone)" if undone else "")

                if undone:
                    try:
                        proof, = self._database.execute(u"SELECT packet FROM sync WHERE id = ?", (undone,)).next()
                    except StopIteration:
                        pass
                    else:
                        self._statistics.dict_inc(self._statistics.outgoing, u"-duplicate-undo-")
                        self._endpoint.send([message.candidate], [str(proof)])

            else:
                signature_length = message.authentication.member.signature_length
                if have_packet[:signature_length] == message.packet[:signature_length]:
                    # the message payload is binary unique (only the signature is different)
                    logger.warning("received identical message %s %d@%d with different signature from %s %s",
                                   message.name,
                                   message.authentication.member.database_id,
                                   message.distribution.global_time,
                                   message.candidate,
                                   "(this message is undone)" if undone else "")

                    if have_packet < message.packet:
                        # replace our current message with the other one
                        self._database.execute(u"UPDATE sync SET packet = ? WHERE community = ? AND member = ? AND global_time = ?",
                                               (buffer(message.packet), community.database_id, message.authentication.member.database_id, message.distribution.global_time))

                        # notify that global times have changed
                        # community.update_sync_range(message.meta, [message.distribution.global_time])

                else:
                    logger.warning("received message with duplicate community/member/global-time triplet from %s.  possibly malicious behaviour", message.candidate)

            # this message is a duplicate
            return True

    def _check_full_sync_distribution_batch(self, messages):
        """
        Ensure that we do not yet have the messages and that, if sequence numbers are enabled, we
        are not missing any previous messages.

        This method is called when a batch of messages with the FullSyncDistribution policy is
        received.  Duplicate messages will yield DropMessage.  And if enable_sequence_number is
        True, missing messages will yield the DelayMessageBySequence exception.

        @param messages: The messages that are to be checked.
        @type message: [Message.Implementation]

        @return: A generator with messages, DropMessage, or DelayMessageBySequence instances
        @rtype: [Message.Implementation|DropMessage|DelayMessageBySequence]
        """
        assert isinstance(messages, list)
        assert len(messages) > 0
        assert all(isinstance(message, Message.Implementation) for message in messages)
        assert all(message.community == messages[0].community for message in messages)
        assert all(message.meta == messages[0].meta for message in messages)

        # a message is considered unique when (creator, global-time),
        # i.e. (authentication.member.database_id, distribution.global_time), is unique.
        unique = set()
        execute = self._database.execute
        enable_sequence_number = messages[0].meta.distribution.enable_sequence_number

        # sort the messages by their (1) global_time and (2) binary packet
        messages = sorted(messages, lambda a, b: cmp(a.distribution.global_time, b.distribution.global_time) or cmp(a.packet, b.packet))

        # refuse messages where the global time is unreasonably high
        acceptable_global_time = messages[0].community.acceptable_global_time

        if enable_sequence_number:
            # obtain the highest sequence_number from the database
            highest = {}
            for message in messages:
                if not message.authentication.member.database_id in highest:
                    last_global_time, seq = execute(u"SELECT MAX(global_time), COUNT(*) FROM sync WHERE member = ? AND meta_message = ?",
                                                    (message.authentication.member.database_id, message.database_id)).next()
                    highest[message.authentication.member.database_id] = (last_global_time or 0, seq)

            # all messages must follow the sequence_number order
            for message in messages:
                if message.distribution.global_time > acceptable_global_time:
                    yield DropMessage(message, "global time is not within acceptable range (%d, we accept %d)" % (message.distribution.global_time, acceptable_global_time))
                    continue

                if not message.distribution.pruning.is_active():
                    yield DropMessage(message, "message has been pruned")
                    continue

                key = (message.authentication.member.database_id, message.distribution.global_time)
                if key in unique:
                    yield DropMessage(message, "duplicate message by member^global_time (1)")
                    continue

                unique.add(key)
                last_global_time, seq = highest[message.authentication.member.database_id]

                if seq >= message.distribution.sequence_number:
                    # we already have this message (drop)

                    # fetch the corresponding packet from the database (it should be binary identical)
                    global_time, packet = execute(u"SELECT global_time, packet FROM sync WHERE member = ? AND meta_message = ? ORDER BY global_time, packet LIMIT 1 OFFSET ?",
                                                  (message.authentication.member.database_id, message.database_id, message.distribution.sequence_number - 1)).next()
                    packet = str(packet)
                    if message.packet == packet:
                        yield DropMessage(message, "duplicate message by binary packet")
                        continue

                    else:
                        # we already have a message with this sequence number, but apparently both
                        # are signed/valid.  we need to discard one of them
                        if (global_time, packet) < (message.distribution.global_time, message.packet):
                            # we keep PACKET (i.e. the message that we currently have in our database)
                            yield DropMessage(message, "duplicate message by sequence number (1)")
                            continue

                        else:
                            # TODO we should undo the messages that we are about to remove (when applicable)
                            execute(u"DELETE FROM sync WHERE member = ? AND meta_message = ? AND global_time >= ?",
                                    (message.authentication.member.database_id, message.database_id, global_time))
                            logger.debug("removed %d entries from sync because the member created multiple sequences", self._database.changes)

                            # by deleting messages we changed SEQ and the HIGHEST cache
                            last_global_time, seq = execute(u"SELECT MAX(global_time), COUNT(*) FROM sync WHERE member = ? AND meta_message = ?",
                                                           (message.authentication.member.database_id, message.database_id)).next()
                            highest[message.authentication.member.database_id] = (last_global_time or 0, seq)
                            # we can allow MESSAGE to be processed

                if seq + 1 != message.distribution.sequence_number:
                    # we do not have the previous message (delay and request)
                    yield DelayMessageBySequence(message, seq + 1, message.distribution.sequence_number - 1)
                    continue

                # we have the previous message, check for duplicates based on community,
                # member, and global_time
                if self._is_duplicate_sync_message(message):
                    # we have the previous message (drop)
                    yield DropMessage(message, "duplicate message by global_time (1)")
                    continue

                # ensure that MESSAGE.distribution.global_time > LAST_GLOBAL_TIME
                if last_global_time and message.distribution.global_time <= last_global_time:
                    logger.debug("last_global_time: %d  message @%d", last_global_time, message.distribution.global_time)
                    yield DropMessage(message, "higher sequence number with lower global time than most recent message")
                    continue

                # we accept this message
                highest[message.authentication.member.database_id] = (message.distribution.global_time, seq + 1)
                yield message

        else:
            for message in messages:
                if message.distribution.global_time > acceptable_global_time:
                    yield DropMessage(message, "global time is not within acceptable range")
                    continue

                if not message.distribution.pruning.is_active():
                    yield DropMessage(message, "message has been pruned")
                    continue

                key = (message.authentication.member.database_id, message.distribution.global_time)
                if key in unique:
                    yield DropMessage(message, "duplicate message by member^global_time (2)")
                    continue

                unique.add(key)

                # check for duplicates based on community, member, and global_time
                if self._is_duplicate_sync_message(message):
                    # we have the previous message (drop)
                    yield DropMessage(message, "duplicate message by global_time (2)")
                    continue

                # we accept this message
                yield message

    def _check_last_sync_distribution_batch(self, messages):
        """
        Check that the messages do not violate any database consistency rules.

        This method is called when a batch of messages with the LastSyncDistribution policy is
        received.  An iterator will be returned where each element is either: DropMessage (for
        duplicate and old messages), DelayMessage (for messages that requires something before they
        can be processed), or Message.Implementation when the message does not violate any rules.

        The rules:

         - The combination community, member, global_time must be unique.

         - When the MemberAuthentication policy is used: the message owner may not have more than
           history_size messages in the database at any one time.  Hence, if this limit is reached
           and the new message is older than the older message that is already available, it is
           dropped.

         - When the DoubleMemberAuthentication policy is used: the members that signed the message
           may not have more than history_size messages in the database at any one time.  Hence, if
           this limit is reached and the new message is older than the older message that is already
           available, it is dropped.  Note that the signature order is not important.

        @param messages: The messages that are to be checked.
        @type message: [Message.Implementation]

        @return: A generator with Message.Implementation or DropMessage instances
        @rtype: [Message.Implementation|DropMessage]
        """
        assert isinstance(messages, list)
        assert len(messages) > 0
        assert all(isinstance(message, Message.Implementation) for message in messages)
        assert all(message.community == messages[0].community for message in messages)
        assert all(message.meta == messages[0].meta for message in messages)
        assert all(isinstance(message.authentication, (MemberAuthentication.Implementation, DoubleMemberAuthentication.Implementation)) for message in messages)

        def check_member_and_global_time(unique, times, message):
            """
            The member + global_time combination must always be unique in the database
            """
            assert isinstance(unique, set)
            assert isinstance(times, dict)
            assert isinstance(message, Message.Implementation)
            assert isinstance(message.distribution, LastSyncDistribution.Implementation)

            key = (message.authentication.member.database_id, message.distribution.global_time)
            if key in unique:
                return DropMessage(message, "already processed message by member^global_time")

            else:
                unique.add(key)

                if not message.authentication.member.database_id in times:
                    times[message.authentication.member.database_id] = [global_time for global_time, in self._database.execute(u"SELECT global_time FROM sync WHERE community = ? AND member = ? AND meta_message = ?",
                                                                                                                               (message.community.database_id, message.authentication.member.database_id, message.database_id))]
                    assert len(times[message.authentication.member.database_id]) <= message.distribution.history_size, [message.packet_id, message.distribution.history_size, times[message.authentication.member.database_id]]
                tim = times[message.authentication.member.database_id]

                if message.distribution.global_time in tim and self._is_duplicate_sync_message(message):
                    return DropMessage(message, "duplicate message by member^global_time (3)")

                elif len(tim) >= message.distribution.history_size and min(tim) > message.distribution.global_time:
                    # we have newer messages (drop)

                    # if the history_size is one, we can send that on message back because
                    # apparently the sender does not have this message yet
                    if message.distribution.history_size == 1:
                        try:
                            packet, = self._database.execute(u"SELECT packet FROM sync WHERE community = ? AND member = ? ORDER BY global_time DESC LIMIT 1",
                                                             (message.community.database_id, message.authentication.member.database_id)).next()
                        except StopIteration:
                            # TODO can still fail when packet is in one of the received messages
                            # from this batch.
                            pass
                        else:
                            self._statistics.dict_inc(self._statistics.outgoing, u"-sequence-")
                            self._endpoint.send([message.candidate], [str(packet)])

                    return DropMessage(message, "old message by member^global_time")

                else:
                    # we accept this message
                    tim.append(message.distribution.global_time)
                    return message

        def check_double_member_and_global_time(unique, times, message):
            """
            No other message may exist with this message.authentication.members / global_time
            combination, regardless of the ordering of the members
            """
            assert isinstance(unique, set)
            assert isinstance(times, dict)
            assert isinstance(message, Message.Implementation)
            assert isinstance(message.authentication, DoubleMemberAuthentication.Implementation)

            key = (message.authentication.member.database_id, message.distribution.global_time)
            if key in unique:
                logger.debug("drop %s %d@%d (in unique)", message.name, message.authentication.member.database_id, message.distribution.global_time)
                return DropMessage(message, "already processed message by member^global_time")

            else:
                unique.add(key)

                members = tuple(sorted(member.database_id for member in message.authentication.members))
                key = members + (message.distribution.global_time,)
                if key in unique:
                    logger.debug("drop %s %s@%d (in unique)", message.name, members, message.distribution.global_time)
                    return DropMessage(message, "already processed message by members^global_time")

                else:
                    unique.add(key)

                    if self._is_duplicate_sync_message(message):
                        # we have the previous message (drop)
                        logger.debug("drop %s %s@%d (_is_duplicate_sync_message)", message.name, members, message.distribution.global_time)
                        return DropMessage(message, "duplicate message by member^global_time (4)")

                    if not members in times:
                        # the next query obtains a list with all global times that we have in the
                        # database for all message.meta messages that were signed by
                        # message.authentication.members where the order of signing is not taken
                        # into account.
                        times[members] = dict((global_time, (packet_id, str(packet)))
                                              for global_time, packet_id, packet
                                              in self._database.execute(u"""
SELECT sync.global_time, sync.id, sync.packet
FROM sync
JOIN double_signed_sync ON double_signed_sync.sync = sync.id
WHERE sync.meta_message = ? AND double_signed_sync.member1 = ? AND double_signed_sync.member2 = ?
""",
                                                                        (message.database_id,) + members))
                        assert len(times[members]) <= message.distribution.history_size, [len(times[members]), message.distribution.history_size]
                    tim = times[members]

                    if message.distribution.global_time in tim:
                        packet_id, have_packet = tim[message.distribution.global_time]

                        if message.packet == have_packet:
                            # exact binary duplicate, do NOT process the message
                            logger.debug("received identical message %s %s@%d from %s", message.name, members, message.distribution.global_time, message.candidate)
                            return DropMessage(message, "duplicate message by binary packet (1)")

                        else:
                            signature_length = sum(member.signature_length for member in message.authentication.members)
                            member_authentication_begin = 23  # version, version, community-id, message-type
                            member_authentication_end = member_authentication_begin + 20 * len(message.authentication.members)
                            if (have_packet[:member_authentication_begin] == message.packet[:member_authentication_begin] and
                                    have_packet[member_authentication_end:signature_length] == message.packet[member_authentication_end:signature_length]):
                                # the message payload is binary unique (only the member order or signatures are different)
                                logger.debug("received identical message with different member-order or signatures %s %s@%d from %s", message.name, members, message.distribution.global_time, message.candidate)

                                if have_packet < message.packet:
                                    # replace our current message with the other one
                                    self._database.execute(u"UPDATE sync SET member = ?, packet = ? WHERE id = ?",
                                                           (message.authentication.member.database_id, buffer(message.packet), packet_id))

                                    return DropMessage(message, "replaced existing packet with other packet with the same payload")

                                return DropMessage(message, "not replacing existing packet with other packet with the same payload")

                            else:
                                logger.warning("received message with duplicate community/members/global-time triplet from %s.  possibly malicious behavior", message.candidate)
                                return DropMessage(message, "duplicate message by binary packet (2)")

                    elif len(tim) >= message.distribution.history_size and min(tim) > message.distribution.global_time:
                        # we have newer messages (drop)

                        # if the history_size is one, we can sent that on message back because
                        # apparently the sender does not have this message yet
                        if message.distribution.history_size == 1:
                            packet_id, have_packet = tim.values()[0]
                            self._statistics.dict_inc(self._statistics.outgoing, u"-sequence-")
                            self._endpoint.send([message.candidate], [have_packet])

                        logger.debug("drop %s %s@%d (older than %s)", message.name, members, message.distribution.global_time, min(tim))
                        return DropMessage(message, "old message by members^global_time")

                    else:
                        # we accept this message
                        logger.debug("accept %s %s@%d", message.name, members, message.distribution.global_time)
                        tim[message.distribution.global_time] = (0, message.packet)
                        return message

        # meta message
        meta = messages[0].meta

        # sort the messages by their (1) global_time and (2) binary packet
        messages = sorted(messages, lambda a, b: cmp(a.distribution.global_time, b.distribution.global_time) or cmp(a.packet, b.packet))

        # refuse messages where the global time is unreasonably high
        acceptable_global_time = meta.community.acceptable_global_time
        messages = [message if message.distribution.global_time <= acceptable_global_time else DropMessage(message, "global time is not within acceptable range") for message in messages]

        # refuse messages that have been pruned (or soon will be)
        messages = [DropMessage(message, "message has been pruned") if isinstance(message, Message.Implementation) and not message.distribution.pruning.is_active() else message for message in messages]

        if isinstance(meta.authentication, MemberAuthentication):
            # a message is considered unique when (creator, global-time), i.r. (authentication.member,
            # distribution.global_time), is unique.  UNIQUE is used in the check_member_and_global_time
            # function
            unique = set()
            times = {}
            messages = [message if isinstance(message, DropMessage) else check_member_and_global_time(unique, times, message) for message in messages]

        # instead of storing HISTORY_SIZE messages for each authentication.member, we will store
        # HISTORY_SIZE messages for each combination of authentication.members.
        else:
            assert isinstance(meta.authentication, DoubleMemberAuthentication)
            unique = set()
            times = {}
            messages = [message if isinstance(message, DropMessage) else check_double_member_and_global_time(unique, times, message) for message in messages]

        return messages

    def _check_direct_distribution_batch(self, messages):
        """
        Returns the messages in the correct processing order.

        This method is called when a message with the DirectDistribution policy is received.  This
        message is not stored and hence we will not be able to see if we have already received this
        message.

        Receiving the same DirectDistribution multiple times indicates that the sending -wanted- to
        send this message multiple times.

        @param messages: Ignored.
        @type messages: [Message.Implementation]

        @return: All messages that are not dropped, i.e. all messages
        @rtype: [Message.Implementation]
        """
        # sort the messages by their (1) global_time and (2) binary packet
        messages = sorted(messages, lambda a, b: cmp(a.distribution.global_time, b.distribution.global_time) or cmp(a.packet, b.packet))

        # direct messages tell us what other people believe is the current global_time
        community = messages[0].community
        for message in messages:
            if isinstance(message.candidate, WalkCandidate):
                message.candidate.global_time = message.distribution.global_time

        return messages

    def load_message(self, community, member, global_time, verify=False):
        """
        Returns the message identified by community, member, and global_time.

        Each message is uniquely identified by the community that it is created in, the member it is
        created by and the global time when it is created.  Using these three parameters we return
        the associated the Message.Implementation instance.  None is returned when we do not have
        this message or it can not be decoded.
        """
        try:
            packet_id, packet = self._database.execute(u"SELECT id, packet FROM sync WHERE community = ? AND member = ? AND global_time = ? LIMIT 1",
                                                       (community.database_id, member.database_id, global_time)).next()
        except StopIteration:
            return None

        # find associated conversion
        try:
            conversion = community.get_conversion_for_packet(packet)
        except KeyError:
            logger.warning("unable to convert a %d byte packet (unknown conversion)", len(packet))
            return None

        # attempt conversion
        try:
            message = conversion.decode_message(LoopbackCandidate(), packet, verify)

        except (DropPacket, DelayPacket) as exception:
            logger.warning("unable to convert a %d byte packet (%s)", len(packet), exception)
            return None

        message.packet_id = packet_id
        return message

    def convert_packet_to_meta_message(self, packet, community=None, load=True, auto_load=True):
        """
        Returns the Message representing the packet or None when no conversion is possible.
        """
        if __debug__:
            from .community import Community
        assert isinstance(packet, str)
        assert isinstance(community, (type(None), Community))
        assert isinstance(load, bool)
        assert isinstance(auto_load, bool)

        # find associated community
        if not community:
            try:
                community = self.get_community(packet[2:22], load, auto_load)
            except KeyError:
                logger.warning("unable to convert a %d byte packet (unknown community)", len(packet))
                return None

        # find associated conversion
        try:
            conversion = community.get_conversion_for_packet(packet)
        except KeyError:
            logger.warning("unable to convert a %d byte packet (unknown conversion)", len(packet))
            return None

        try:
            return conversion.decode_meta_message(packet)

        except (DropPacket, DelayPacket) as exception:
            logger.warning("unable to convert a %d byte packet (%s)", len(packet), exception)
            return None

    def convert_packet_to_message(self, packet, community=None, load=True, auto_load=True, candidate=None, verify=True):
        """
        Returns the Message.Implementation representing the packet or None when no conversion is
        possible.
        """
        if __debug__:
            from .community import Community
        assert isinstance(packet, str), type(packet)
        assert community is None or isinstance(community, Community), type(community)
        assert isinstance(load, bool), type(load)
        assert isinstance(auto_load, bool), type(auto_load)
        assert candidate is None or isinstance(candidate, Candidate), type(candidate)

        # find associated community
        if not community:
            try:
                community = self.get_community(packet[2:22], load, auto_load)
            except KeyError:
                logger.warning("unable to convert a %d byte packet (unknown community)", len(packet))
                return None

        # find associated conversion
        try:
            conversion = community.get_conversion_for_packet(packet)
        except KeyError:
            logger.warning("unable to convert a %d byte packet (unknown conversion)", len(packet))
            return None

        try:
            return conversion.decode_message(LoopbackCandidate() if candidate is None else candidate, packet, verify)

        except (DropPacket, DelayPacket) as exception:
            logger.warning("unable to convert a %d byte packet (%s)", len(packet), exception)
            return None

    def convert_packets_to_messages(self, packets, community=None, load=True, auto_load=True, candidate=None, verify=True):
        """
        Returns a list with messages representing each packet or None when no conversion is
        possible.
        """
        assert isinstance(packets, (list, tuple)), type(packets)
        assert all(isinstance(packet, str) for packet in packets), [type(packet) for packet in packets]
        return [self.convert_packet_to_message(packet, community, load, auto_load, candidate, verify) for packet in packets]

    def on_incoming_packets(self, packets, cache=True, timestamp=0.0):
        """
        Process incoming UDP packets.

        This method is called to process one or more UDP packets.  This occurs when new packets are
        received, to attempt to process previously delayed packets, or when a member explicitly
        creates a packet to process.  The last option should only occur for debugging purposes.

        The following steps are followed:

        1. Group the packets by community.

        2. Try to obtain the community.

        3. In case 2 suceeded: Pass the packets to the community for further processing.

        """
        assert isinstance(packets, (tuple, list)), packets
        assert len(packets) > 0, packets
        assert all(isinstance(packet, tuple) for packet in packets), packets
        assert all(len(packet) == 2 for packet in packets), packets
        assert all(isinstance(packet[0], Candidate) for packet in packets), packets
        assert all(isinstance(packet[1], str) for packet in packets), packets
        assert isinstance(cache, bool), cache
        assert isinstance(timestamp, float), timestamp

        self._statistics.received_count += len(packets)

        sort_key = lambda tup: (tup[1][2:22], tup[1][22])  # community ID, message meta type
        groupby_key = lambda tup: tup[1][2:22]  # community ID
        for community_id, iterator in groupby(sorted(packets, key=sort_key), key=groupby_key):
            # find associated community
            try:
                community = self.get_community(community_id)
                community.on_incoming_packets(list(iterator), cache, timestamp)
            except KeyError:
                packets = list(iterator)
                candidates = set([candidate for candidate, _ in packets])
                logger.warning("drop %d packets (received packet(s) for unknown community): %s", len(packets), map(str, candidates))
                self._statistics.dict_inc(self._statistics.drop, "_convert_packets_into_batch:unknown community")
                self._statistics.drop_count += 1

    def _store(self, messages):
        """
        Store a message in the database.

        Messages with the Last- or Full-SyncDistribution policies need to be stored in the database
        to allow them to propagate to other members.

        Messages with the LastSyncDistribution policy may also cause an older message to be removed
        from the database.

        Messages created by a member that we have marked with must_store will also be stored in the
        database, and hence forwarded to others.

        @param message: The unstored message with the SyncDistribution policy.
        @type message: Message.Implementation
        """
        assert isinstance(messages, list)
        assert len(messages) > 0
        assert all(isinstance(message, Message.Implementation) for message in messages)
        assert all(message.community == messages[0].community for message in messages)
        assert all(message.meta == messages[0].meta for message in messages)
        assert all(isinstance(message.distribution, SyncDistribution.Implementation) for message in messages)
        # ensure no duplicate messages are present, this MUST HAVE been checked before calling this
        # method!
        assert len(messages) == len(set((message.authentication.member.database_id, message.distribution.global_time) for message in messages)), messages[0].name

        meta = messages[0].meta
        logger.debug("attempting to store %d %s messages", len(messages), meta.name)
        is_double_member_authentication = isinstance(meta.authentication, DoubleMemberAuthentication)
        highest_global_time = 0

        # update_sync_range = set()
        for message in messages:
            # the signature must be set
            assert isinstance(message.authentication, (MemberAuthentication.Implementation, DoubleMemberAuthentication.Implementation)), message.authentication
            assert message.authentication.is_signed
            assert not message.packet[-10:] == "\x00" * 10, message.packet[-10:].encode("HEX")
            # we must have the identity message as well
            assert message.authentication.encoding == "bin" or message.authentication.member.has_identity(message.community), [message, message.community, message.authentication.member.database_id]

            logger.debug("%s %d@%d", message.name, message.authentication.member.database_id, message.distribution.global_time)

            # add packet to database
            self._database.execute(u"INSERT INTO sync (community, member, global_time, meta_message, packet) VALUES (?, ?, ?, ?, ?)",
                                  (message.community.database_id,
                                   message.authentication.member.database_id,
                                   message.distribution.global_time,
                                   message.database_id,
                                   buffer(message.packet)))
            # update_sync_range.add(message.distribution.global_time)
            if __debug__:
                # must have stored one entry
                assert self._database.changes == 1
                # when sequence numbers are enabled, we must have exactly
                # message.distribution.sequence_number messages in the database
                if isinstance(message.distribution, FullSyncDistribution) and message.distribution.enable_sequence_number:
                    count_ = self._database.execute(u"SELECT COUNT(*) FROM sync WHERE meta_message = ? AND member = ?", (message.database_id, message.authentication.member.database_id)).next()
                    assert count_ == message.distribution.sequence_number, [count_, message.distribution.sequence_number]

            # ensure that we can reference this packet
            message.packet_id = self._database.last_insert_rowid
            logger.debug("stored message %s in database at row %d", message.name, message.packet_id)

            if is_double_member_authentication:
                member1 = message.authentication.members[0].database_id
                member2 = message.authentication.members[1].database_id
                self._database.execute(u"INSERT INTO double_signed_sync (sync, member1, member2) VALUES (?, ?, ?)",
                                       (message.packet_id, member1, member2) if member1 < member2 else (message.packet_id, member2, member1))
                assert self._database.changes == 1

            # update global time
            highest_global_time = max(highest_global_time, message.distribution.global_time)

        if isinstance(meta.distribution, LastSyncDistribution):
            # delete packets that have become obsolete
            items = set()
            if is_double_member_authentication:
                order = lambda member1, member2: (member1, member2) if member1 < member2 else (member2, member1)
                for member1, member2 in set(order(message.authentication.members[0].database_id, message.authentication.members[1].database_id) for message in messages):
                    assert member1 < member2, [member1, member2]
                    all_items = list(self._database.execute(u"""
SELECT sync.id, sync.global_time
FROM sync
JOIN double_signed_sync ON double_signed_sync.sync = sync.id
WHERE sync.meta_message = ? AND double_signed_sync.member1 = ? AND double_signed_sync.member2 = ?
ORDER BY sync.global_time, sync.packet""", (meta.database_id, member1, member2)))
                    if len(all_items) > meta.distribution.history_size:
                        items.update(all_items[:len(all_items) - meta.distribution.history_size])

            else:
                for member_database_id in set(message.authentication.member.database_id for message in messages):
                    all_items = list(self._database.execute(u"""
SELECT id, global_time
FROM sync
WHERE meta_message = ? AND member = ?
ORDER BY global_time""", (meta.database_id, member_database_id)))
                    if len(all_items) > meta.distribution.history_size:
                        items.update(all_items[:len(all_items) - meta.distribution.history_size])

            if items:
                self._database.executemany(u"DELETE FROM sync WHERE id = ?", [(syncid,) for syncid, _ in items])
                assert len(items) == self._database.changes
                logger.debug("deleted %d messages", self._database.changes)

                if is_double_member_authentication:
                    self._database.executemany(u"DELETE FROM double_signed_sync WHERE sync = ?", [(syncid,) for syncid, _ in items])
                    assert len(items) == self._database.changes

                # update_sync_range.update(global_time for _, _, global_time in items)

            # 12/10/11 Boudewijn: verify that we do not have to many packets in the database
            if __debug__:
                if not is_double_member_authentication:
                    for message in messages:
                        history_size, = self._database.execute(u"SELECT COUNT(*) FROM sync WHERE meta_message = ? AND member = ?", (message.database_id, message.authentication.member.database_id)).next()
                        assert history_size <= message.distribution.history_size, [count, message.distribution.history_size, message.authentication.member.database_id]

        # update the global time
        meta.community.update_global_time(highest_global_time)

        meta.community.dispersy_store(messages)

        # if update_sync_range:
        # notify that global times have changed
        #     meta.community.update_sync_range(meta, update_sync_range)

    @property
    def bootstrap_candidates(self):
        return self._bootstrap_candidates.itervalues()

    def estimate_lan_and_wan_addresses(self, sock_addr, lan_address, wan_address):
        """
        We received a message from SOCK_ADDR claiming to have LAN_ADDRESS and WAN_ADDRESS, returns
        the estimated LAN and WAN address for this node.

        The returns LAN and WAN addresses are either modified when we know they are incorrect (based
        on the reported sock_addr) or they remain unchanged.  Hence the returned addresses may be
        ("0.0.0.0", 0).
        """
        assert self.is_valid_address(sock_addr), sock_addr

        if any(sock_addr[0] in interface for interface in self._local_interfaces):
            # is SOCK_ADDR is on our local LAN, hence LAN_ADDRESS should be SOCK_ADDR
            if sock_addr != lan_address:
                logger.debug("estimate someones LAN address is %s (LAN was %s, WAN stays %s)",
                             sock_addr, lan_address, wan_address)
                lan_address = sock_addr

        else:
            # is SOCK_ADDR is outside our local LAN, hence WAN_ADDRESS should be SOCK_ADDR
            if sock_addr != wan_address:
                logger.info("estimate someones WAN address is %s (WAN was %s, LAN stays %s)",
                            sock_addr, wan_address, lan_address)
                wan_address = sock_addr

        return lan_address, wan_address

    def store_update_forward(self, messages, store, update, forward):
        """
        Usually we need to do three things when we have a valid messages: (1) store it in our local
        database, (2) process the message locally by calling the handle_callback method, and (3)
        forward the message to other nodes in the community.  This method is a shorthand for doing
        those three tasks.

        To reduce the disk activity, namely syncing the database to disk, we will perform the
        database commit not after the (1) store operation but after the (2) update operation.  This
        will ensure that any database changes from handling the message are also synced to disk.  It
        is important to note that the sync will occur before the (3) forward operation to ensure
        that no remote nodes will obtain data that we have not safely synced ourselves.

        For performance reasons messages are processed in batches, where each batch contains only
        messages from the same community and the same meta message instance.  This method, or more
        specifically the methods that handle the actual storage, updating, and forwarding, assume
        this clustering.

        @param messages: A list with the messages that need to be stored, updated, and forwarded.
         All messages need to be from the same community and meta message instance.
        @type messages: [Message.Implementation]

        @param store: When True the messages are stored (as defined by their message distribution
         policy) in the local dispersy database.  This parameter should (almost always) be True, its
         inclusion is mostly to allow certain debugging scenarios.
        @type store: bool

        @param update: When True the messages are passed to their handle_callback methods.  This
         parameter should (almost always) be True, its inclusion is mostly to allow certain
         debugging scenarios.
        @type update: bool

        @param forward: When True the messages are forwarded (as defined by their message
         destination policy) to other nodes in the community.  This parameter should (almost always)
         be True, its inclusion is mostly to allow certain debugging scenarios.
        @type store: bool
        """
        assert isinstance(messages, list)
        assert len(messages) > 0
        assert all(isinstance(message, Message.Implementation) for message in messages)
        assert all(message.community == messages[0].community for message in messages)
        assert all(message.meta == messages[0].meta for message in messages)
        assert isinstance(store, bool)
        assert isinstance(update, bool)
        assert isinstance(forward, bool)

        logger.debug("%d %s messages (%s %s %s)", len(messages), messages[0].name, store, update, forward)

        store = store and isinstance(messages[0].meta.distribution, SyncDistribution)
        if store:
            self._store(messages)

        if update:
            try:
                messages[0].handle_callback(messages)
            except (SystemExit, KeyboardInterrupt, GeneratorExit, AssertionError):
                raise
            except:
                print_exc()
                logger.exception("exception during handle_callback for %s", messages[0].name)
                return False

        # 07/10/11 Boudewijn: we will only commit if it the message was create by our self.
        # Otherwise we can safely skip the commit overhead, since, if a crash occurs, we will be
        # able to regain the data eventually
        if store:
            my_messages = sum(message.authentication.member == message.community.my_member for message in messages)
            if my_messages:
                logger.debug("commit user generated message")
                self._database.commit()

                self._statistics.created_count += my_messages
                self._statistics.dict_inc(self._statistics.created, messages[0].meta.name, my_messages)

        if forward:
            return self._forward(messages)

        return True

    def _forward(self, messages):
        """
        Queue a sequence of messages to be sent to other members.

        First all messages that use the SyncDistribution policy are stored to the database to allow
        them to propagate when a dispersy-sync message is received.

        Second all messages are sent depending on their destination policy:

         - CandidateDestination causes a message to be sent to the addresses in
           message.destination.candidates.

         - CommunityDestination causes a message to be sent to one or more addresses to be picked
           from the database candidate table.

        @param messages: A sequence with one or more messages.
        @type messages: [Message.Implementation]
        """
        assert isinstance(messages, (tuple, list))
        assert len(messages) > 0
        assert all(isinstance(message, Message.Implementation) for message in messages)
        assert all(message.community == messages[0].community for message in messages)
        assert all(message.meta == messages[0].meta for message in messages)

        result = True
        meta = messages[0].meta
        if isinstance(meta.destination, (CommunityDestination, CandidateDestination)):
            for message in messages:
                # CandidateDestination.candidates may be empty
                candidates = set(message.destination.candidates)
                # CommunityDestination.node_count is allowed to be zero
                if isinstance(meta.destination, CommunityDestination) and meta.destination.node_count > 0:
                    max_candidates = meta.destination.node_count + len(candidates)
                    for candidate in meta.community.dispersy_yield_verified_candidates():
                        if len(candidates) < max_candidates:
                            candidates.add(candidate)
                        else:
                            break
                result = result and self._send(tuple(candidates), [message])
        else:
            raise NotImplementedError(meta.destination)

        return result

    def _send(self, candidates, messages, debug=False):
        """
        Send a list of messages to a list of candidates. If no candidates are specified or endpoint reported
        a failure this method will return False.

        @param candidates: A sequence with one or more candidates.
        @type candidates: [Candidate]

        @param messages: A sequence with one or more messages.
        @type messages: [Message.Implementation]
        """
        assert isinstance(candidates, (tuple, list, set)), type(candidates)
        # 04/03/13 boudewijn: CANDIDATES should contain candidates, never None
        # candidates = [candidate for candidate in candidates if candidate]
        assert all(isinstance(candidate, Candidate) for candidate in candidates)
        assert isinstance(messages, (tuple, list))
        assert len(messages) > 0
        assert all(isinstance(message, Message.Implementation) for message in messages)

        messages_send = False
        if len(candidates) and len(messages):
            packets = [message.packet for message in messages]
            messages_send = self._endpoint.send(candidates, packets)

        if messages_send:
            for message in messages:
                self._statistics.dict_inc(self._statistics.outgoing, message.meta.name, len(candidates))

        return messages_send

    def declare_malicious_member(self, member, packets):
        """
        Provide one or more signed messages that prove that the creator is malicious.

        The messages are stored separately as proof that MEMBER is malicious, furthermore, all other
        messages that MEMBER created are removed from the dispersy database (limited to one
        community) to prevent further spreading of its data.

        Furthermore, whenever data is received that is signed by a malicious member, the incoming
        data is ignored and the proof is given to the sender to allow her to prevent her from
        forwarding any more data.

        Finally, the community is notified.  The community can choose what to do, however, it is
        important to note that messages from the malicious member are no longer propagated.  Hence,
        unless all traces from the malicious member are removed, no global consensus can ever be
        achieved.

        @param member: The malicious member.
        @type member: Member

        @param packets: One or more packets proving that the member is malicious.  All packets must
         be associated to the same community.
        @type packets: [Packet]
        """
        if __debug__:
            assert isinstance(member, Member)
            assert not member.must_blacklist, "must not already be blacklisted"
            assert isinstance(packets, list)
            assert len(packets) > 0
            assert all(isinstance(packet, Packet) for packet in packets)
            assert all(packet.meta == packets[0].meta for packet in packets)

        logger.debug("proof based on %d packets", len(packets))

        # notify the community
        community = packets[0].community
        community.dispersy_malicious_member_detected(member, packets)

        # set the member blacklisted tag
        member.must_blacklist = True

        # store the proof
        self._database.executemany(u"INSERT INTO malicious_proof (community, member, packet) VALUES (?, ?, ?)",
                                   ((community.database_id, member.database_id, buffer(packet.packet)) for packet in packets))

        # remove all messages created by the malicious member
        self._database.execute(u"DELETE FROM sync WHERE community = ? AND member = ?",
                               (community.database_id, member.database_id))

        # TODO: if we have a address for the malicious member, we can also remove her from the
        # candidate table

    def send_malicious_proof(self, community, member, candidate):
        """
        If we have proof that MEMBER is malicious in COMMUNITY, usually in the form of one or more
        signed messages, then send this proof to CANDIDATE.

        @param community: The community where member was malicious.
        @type community: Community

        @param member: The malicious member.
        @type member: Member

        @param candidate: The address where we want the proof to be send.
        @type candidate: Candidate
        """
        if __debug__:
            from .community import Community
            assert isinstance(community, Community)
            assert isinstance(member, Member)
            assert member.must_blacklist, "must be blacklisted"
            assert isinstance(candidate, Candidate)

        packets = [str(packet) for packet, in self._database.execute(u"SELECT packet FROM malicious_proof WHERE community = ? AND member = ?",
                                                                     (community.database_id, member.database_id))]
        logger.debug("found %d malicious proof packets, sending to %s", len(packets), candidate)

        if packets:
            self._statistics.dict_inc(self._statistics.outgoing, u"-malicious-proof", len(packets))
            self._endpoint.send([candidate], packets)

    def is_valid_address(self, address):
        """
        Returns True when ADDRESS is valid.

        ADDRESS must be supplied as a (HOST string, PORT integer) tuple.

        An address is valid when it meets the following criteria:
        - HOST must be non empty
        - HOST must be non '0.0.0.0'
        - PORT must be > 0
        - HOST must be 'A.B.C.D' where A, B, and C are numbers higher or equal to 0 and lower or
          equal to 255.  And where D is higher than 0 and lower than 255
        """
        assert isinstance(address, tuple), type(address)
        assert len(address) == 2, len(address)
        assert isinstance(address[0], str), type(address[0])
        assert isinstance(address[1], int), type(address[1])

        if address[0] == "":
            return False

        if address[0] == "0.0.0.0":
            return False

        if address[1] <= 0:
            return False

        try:
            binary = inet_aton(address[0])
        except socket_error:
            return False

        # ending with .0
# Niels: is now allowed, subnet mask magic call actually allow for this
#        if binary[3] == "\x00":
#            return False

        # ending with .255
        if binary[3] == "\xff":
            return False

        return True

    def sanity_check(self, community, test_identity=True, test_undo_other=True, test_binary=False, test_sequence_number=True, test_last_sync=True):
        """
        Check everything we can about a community.

        Note that messages that are disabled, i.e. not included in community.get_meta_messages(),
        will NOT be checked.

        - the dispersy-identity for my member must be in the database
        - the dispersy-identity must be in the database for each member that has one or more messages in the database
        - all packets in the database must be valid
        - check sequence numbers for FullSyncDistribution
        - check history size for LastSyncDistribution
        """
        def select(sql, bindings):
            assert isinstance(sql, unicode)
            assert isinstance(bindings, tuple)
            limit = 1000
            for offset in (i * limit for i in count()):
                rows = list(self._database.execute(sql, bindings + (limit, offset)))
                if rows:
                    for row in rows:
                        yield row
                else:
                    break

        logger.debug("%s start sanity check [database-id:%d]", community.cid.encode("HEX"), community.database_id)
        enabled_messages = set(meta.database_id for meta in community.get_meta_messages())

        if test_identity:
            try:
                meta_identity = community.get_meta_message(u"dispersy-identity")
            except KeyError:
                # identity is not enabled
                pass
            else:
                #
                # ensure that the dispersy-identity for my member must be in the database
                #
                try:
                    member_id, = self._database.execute(u"SELECT id FROM member WHERE mid = ?", (buffer(community.my_member.mid),)).next()
                except StopIteration:
                    raise ValueError("unable to find the public key for my member")

                if not member_id == community.my_member.database_id:
                    raise ValueError("my member's database id is invalid", member_id, community.my_member.database_id)

                try:
                    self._database.execute(u"SELECT 1 FROM private_key WHERE member = ?", (member_id,)).next()
                except StopIteration:
                    raise ValueError("unable to find the private key for my member")

                try:
                    self._database.execute(u"SELECT 1 FROM sync WHERE member = ? AND meta_message = ?", (member_id, meta_identity.database_id)).next()
                except StopIteration:
                    raise ValueError("unable to find the dispersy-identity message for my member")

                logger.debug("my identity is OK")

                #
                # the dispersy-identity must be in the database for each member that has one or more
                # messages in the database
                #
                A = set(id_ for id_, in self._database.execute(u"SELECT member FROM sync WHERE community = ? GROUP BY member", (community.database_id,)))
                B = set(id_ for id_, in self._database.execute(u"SELECT member FROM sync WHERE meta_message = ?", (meta_identity.database_id,)))
                if not len(A) == len(B):
                    raise ValueError("inconsistent dispersy-identity messages.", A.difference(B))

        if test_undo_other:
            try:
                meta_undo_other = community.get_meta_message(u"dispersy-undo-other")
            except KeyError:
                # undo-other is not enabled
                pass
            else:

                #
                # ensure that we have proof for every dispersy-undo-other message
                #
                # TODO we are not taking into account that undo messages can be undone
                for undo_packet_id, undo_packet_global_time, undo_packet in select(u"SELECT id, global_time, packet FROM sync WHERE community = ? AND meta_message = ? ORDER BY id LIMIT ? OFFSET ?", (community.database_id, meta_undo_other.database_id)):
                    undo_packet = str(undo_packet)
                    undo_message = self.convert_packet_to_message(undo_packet, community, verify=False)

                    # 10/10/12 Boudewijn: the check_callback is required to obtain the
                    # message.payload.packet
                    for _ in undo_message.check_callback([undo_message]):
                        pass

                    # get the message that undo_message refers to
                    try:
                        packet, undone = self._database.execute(u"SELECT packet, undone FROM sync WHERE community = ? AND member = ? AND global_time = ?", (community.database_id, undo_message.payload.member.database_id, undo_message.payload.global_time)).next()
                    except StopIteration:
                        raise ValueError("found dispersy-undo-other but not the message that it refers to")
                    packet = str(packet)
                    message = self.convert_packet_to_message(packet, community, verify=False)

                    if not undone:
                        raise ValueError("found dispersy-undo-other but the message that it refers to is not undone")

                    if message.undo_callback is None:
                        raise ValueError("found dispersy-undo-other but the message that it refers to does not have an undo_callback")

                    # get the proof that undo_message is valid
                    allowed, proofs = community.timeline.check(undo_message)

                    if not allowed:
                        raise ValueError("found dispersy-undo-other that, according to the timeline, is not allowed")

                    if not proofs:
                        raise ValueError("found dispersy-undo-other that, according to the timeline, has no proof")

                    logger.debug("dispersy-undo-other packet %d@%d referring %s %d@%d is OK", undo_packet_id, undo_packet_global_time, undo_message.payload.packet.name, undo_message.payload.member.database_id, undo_message.payload.global_time)

        if test_binary:
            #
            # ensure all packets in the database are valid and that the binary packets are consistent
            # with the information stored in the database
            #
            for packet_id, member_id, global_time, meta_message_id, packet in select(u"SELECT id, member, global_time, meta_message, packet FROM sync WHERE community = ? ORDER BY id LIMIT ? OFFSET ?", (community.database_id,)):
                if meta_message_id in enabled_messages:
                    packet = str(packet)
                    message = self.convert_packet_to_message(packet, community, verify=True)

                    if not message:
                        raise ValueError("unable to convert packet ", packet_id, "@", global_time, " to message")

                    if not member_id == message.authentication.member.database_id:
                        raise ValueError("inconsistent member in packet ", packet_id, "@", global_time)

                    if not message.authentication.member.public_key:
                        raise ValueError("missing public key for member ", member_id, " in packet ", packet_id, "@", global_time)

                    if not global_time == message.distribution.global_time:
                        raise ValueError("inconsistent global time in packet ", packet_id, "@", global_time)

                    if not meta_message_id == message.database_id:
                        raise ValueError("inconsistent meta message in packet ", packet_id, "@", global_time)

                    if not packet == message.packet:
                        raise ValueError("inconsistent binary in packet ", packet_id, "@", global_time)

                    logger.debug("packet %d@%d is OK", packet_id, global_time)

        if test_sequence_number:
            for meta in community.get_meta_messages():
                #
                # ensure that we have all sequence numbers for FullSyncDistribution packets
                #
                if isinstance(meta.distribution, FullSyncDistribution) and meta.distribution.enable_sequence_number:
                    counter = 0
                    counter_member_id = 0
                    exception = None
                    for packet_id, member_id, packet in select(u"SELECT id, member, packet FROM sync WHERE meta_message = ? ORDER BY member, global_time LIMIT ? OFFSET ?", (meta.database_id,)):
                        packet = str(packet)
                        message = self.convert_packet_to_message(packet, community, verify=False)
                        assert message

                        if member_id != counter_member_id:
                            counter_member_id = member_id
                            counter = 1
                            if exception:
                                break

                        if not counter == message.distribution.sequence_number:
                            logger.error("%s for member %d has sequence number %d expected %d\n%s", meta.name, member_id, message.distribution.sequence_number, counter, packet.encode("HEX"))
                            exception = ValueError("inconsistent sequence numbers in packet ", packet_id)

                        counter += 1

                    if exception:
                        raise exception

        if test_last_sync:
            for meta in community.get_meta_messages():
                #
                # ensure that we have only history-size messages per member
                #
                if isinstance(meta.distribution, LastSyncDistribution):
                    if isinstance(meta.authentication, MemberAuthentication):
                        counter = 0
                        counter_member_id = 0
                        for packet_id, member_id, packet in select(u"SELECT id, member, packet FROM sync WHERE meta_message = ? ORDER BY member ASC, global_time DESC LIMIT ? OFFSET ?", (meta.database_id,)):
                            message = self.convert_packet_to_message(str(packet), community, verify=False)
                            assert message

                            if member_id == counter_member_id:
                                counter += 1
                            else:
                                counter_member_id = member_id
                                counter = 1

                            if counter > meta.distribution.history_size:
                                raise ValueError("pruned packet ", packet_id, " still in database")

                            logger.debug("LastSyncDistribution for %s is OK", meta.name)

                    else:
                        assert isinstance(meta.authentication, DoubleMemberAuthentication)
                        for packet_id, member_id, packet in select(u"SELECT id, member, packet FROM sync WHERE meta_message = ? ORDER BY member ASC, global_time DESC LIMIT ? OFFSET ?", (meta.database_id,)):
                            message = self.convert_packet_to_message(str(packet), community, verify=False)
                            assert message

                            try:
                                member1, member2 = self._database.execute(u"SELECT member1, member2 FROM double_signed_sync WHERE sync = ?", (packet_id,)).next()
                            except StopIteration:
                                raise ValueError("found double signed message without an entry in the double_signed_sync table")

                            if not member1 < member2:
                                raise ValueError("member1 (", member1, ") must always be smaller than member2 (", member2, ")")

                            if not (member1 == member_id or member2 == member_id):
                                raise ValueError("member1 (", member1, ") or member2 (", member2, ") must be the message creator (", member_id, ")")

                        logger.debug("LastSyncDistribution for %s is OK", meta.name)

        logger.debug("%s success", community.cid.encode("HEX"))

    def _flush_database(self):
        """
        Periodically called to commit database changes to disk.
        """
        while True:
            # 12/07/2012 Arno: apswtrace detects 7 s commits with yield 5 min, so reduce
            # 09/10/2013 Boudewijn: the yield statement should not be inside the try/except (an
            # exception is raised when the _flush_database generator is closed)
            yield 60.0

            try:
                # flush changes to disk every 1 minutes
                self._database.commit()

            except Exception as exception:
                # OperationalError: database is locked
                logger.exception("%s", exception)

    # TODO this -private- method is not used by Dispersy (only from the Tribler SearchGridManager).
    # It can be removed.  The SearchGridManager can call dispersy.database.commit() instead
    def _commit_now(self):
        """
        Flush changes to disk.
        """
        self._database.commit()

    def start(self, timeout=10.0):
        """
        Starts Dispersy.

        This method is thread safe.

        1. starts callback
        2. resolve bootstrap candidates (done in parallel)
        3. opens database
        4. opens endpoint
        """

        assert not self._callback.is_running, "Must be called before callback.start()"
        assert isinstance(timeout, float), type(timeout)
        assert timeout >= 0.0, timeout

        def start():
            assert self._callback.is_current_thread, "Must be called from the callback thread"

            # resolve bootstrap candidates
            self._resolve_bootstrap_candidates(timeout)

            results.append((u"database", self._database.open()))
            assert all(isinstance(result, bool) for _, result in results), [type(result) for _, result in results]

            results.append((u"endpoint", self._endpoint.open(self)))
            assert all(isinstance(result, bool) for _, result in results), [type(result) for _, result in results]
            self._endpoint_ready()

            # commit changes to the database periodically
            id_ = u"flush-database-%d" % (id(self),)
            self._pending_callbacks["flush_database"] = self._callback.register(self._flush_database, id_=id_)
            # output candidate statistics
            id_ = u"dispersy-detailed-candidates-%d" % (id(self),)
            self._pending_callbacks["candidates"] = self._callback.register(self._stats_detailed_candidates, id_=id_)

        # start
        logger.info("starting the Dispersy core...")
        results = []

        results.append((u"callback", self._callback.start()))
        assert all(isinstance(result, bool) for _, result in results), [type(result) for _, result in results]
        self._callback.call(start, priority=512)

        # log and return the result
        if all(result for _, result in results):
            logger.info("Dispersy core ready (database: %s, port:%d)", self._database.file_path, self._endpoint.get_address()[1])
            return True

        else:
            logger.error("Dispersy core unable to start all components [%s]", ", ".join("{0}:{1}".format(key, value) for key, value in results))
            return False

    def stop(self, timeout=10.0):
        """
        Stops Dispersy.

        This method is thread safe.

        1. stops callback
           a. new tasks are no longer accepted
           b. flushes existing tasks
           c. stops existing generators
        2. unload all communities
           in reverse define_auto_load order, starting with all undefined communities
        3. closes endpoint
        4. closes database

        Returns False when Dispersy isn't running, i.e. not callback.is_running, or when one of the
        above steps fails.  Otherwise True is returned.

        Note that attempts will be made to process each step, even if one or more steps fail.  For
        example, when 'close endpoint' reports a failure the databases still be closed.
        """
        assert isinstance(timeout, float), type(timeout)
        assert 0.0 <= timeout, timeout

        def unload_communities(communities):
            for community in communities:
                if community.cid in self._communities:
                    community.unload_community()

        def ordered_unload_communities():
            # unload communities that are not defined
            unload_communities([community
                                for community
                                in self._communities.itervalues()
                                if not community.get_classification() in self._auto_load_communities])

            # unload communities in reverse auto load order
            for classification in reversed(self._auto_load_communities):
                unload_communities([community
                                    for community
                                    in self._communities.itervalues()
                                    if community.get_classification() == classification])

            # stop walking (this should not be necessary, but bugs may cause the walker to keep
            # running and/or be re-started when a community is loaded)
            self._callback.unregister(self._pending_callbacks[u"candidate-walker"])

            return True

        def stop():
            # unload all communities
            results[u"community"] = ordered_unload_communities()

            # stop endpoint
            results[u"endpoint"] = self._endpoint.close(timeout)

            # stop the database
            results[u"database"] = self._database.close()

        if self._callback.is_running:
            # output statistics before we stop
            if logger.isEnabledFor(logging.DEBUG):
                self._statistics.update()
                logger.debug("\n%s", pformat(self._statistics.get_dict(), width=120))

            logger.info("stopping the Dispersy core...")
            results = {u"callback": None, u"community": None, u"endpoint": None, u"database": None}
            results[u"callback"] = self._callback.stop(timeout, final_func=stop)

            # log and return the result
            if all(result for result in results.itervalues()):
                logger.info("Dispersy core properly stopped")
                return True

            else:
                logger.error("Dispersy core unable to stop all components [%s]", results)
                return False

        else:
            logger.warning("Dispersy is already stopping, ignoring second call to Dispersy.stop()")
            return False

    def _candidate_walker(self):
        """
        Periodically select a candidate and take a step in the network.
        """
        walker_communities = self._walker_commmunities

        steps = 0
        start = time()

        # delay will never be less than 0.1, hence we can accommodate 50 communities before the
        # interval between each step becomes larger than 5.0 seconds
        optimaldelay = max(0.1, 5.0 / len(walker_communities))
        logger.debug("there are %d walker enabled communities.  pausing %ss (on average) between each step", len(walker_communities), optimaldelay)

        if __debug__:
            RESETS = 0
            STEPS = 0
            START = start
            DELAY = 0.0
            for community in walker_communities:
                community.__MOST_RECENT_WALK = 0.0

        for community in walker_communities:
            community.__most_recent_sync = 0.0

        while True:
            community = walker_communities.pop(0)
            walker_communities.append(community)

            actualtime = time()
            allow_sync = community.dispersy_enable_bloom_filter_sync and actualtime - community.__most_recent_sync > 4.5
            logger.debug("previous sync was %.1f seconds ago %s", actualtime - community.__most_recent_sync, "" if allow_sync else "(no sync this cycle)")
            if allow_sync:
                community.__most_recent_sync = actualtime

            if __debug__:
                NOW = time()
                OPTIMALSTEPS = (NOW - START) / optimaldelay
                STEPDIFF = NOW - community.__MOST_RECENT_WALK
                community.__MOST_RECENT_WALK = NOW
                logger.debug("%s taking step every %.2fs in %d communities.  steps: %d/%d ~%.2f.  diff: %.1f.  resets: %d",
                             community.cid.encode("HEX"), DELAY, len(walker_communities), steps, int(OPTIMALSTEPS), (-1.0 if OPTIMALSTEPS == 0.0 else (STEPS / OPTIMALSTEPS)), STEPDIFF, RESETS)
                STEPS += 1

            # walk
            assert community.dispersy_enable_candidate_walker
            assert community.dispersy_enable_candidate_walker_responses
            try:
                community.take_step(allow_sync)
                steps += 1
            except Exception:
                logger.exception("%s causes an exception during take_step", community.cid.encode("HEX"))

            optimaltime = start + steps * optimaldelay
            actualtime = time()

            if optimaltime + 5.0 < actualtime:
                # way out of sync!  reset start time
                logger.warning("can not keep up!  resetting walker start time!")
                start = actualtime
                steps = 0
                self._statistics.walk_reset += 1
                if __debug__:
                    DELAY = 0.0
                    RESETS += 1

            else:
                if __debug__:
                    DELAY = max(0.0, optimaltime - actualtime)
                yield max(0.0, optimaltime - actualtime)

    def _stats_detailed_candidates(self):
        """
        Periodically logs a detailed list of all candidates (walk, stumble, intro, none) for all
        communities.

        Enable this output by enabling DEBUG logging for a logger named
        "dispersy-stats-detailed-candidates".

        Exception: all communities with classification "PreviewChannelCommunity" are ignored.
        """
        summary = get_logger("dispersy-stats-detailed-candidates")
        while summary.isEnabledFor(logging.DEBUG):
            yield 5.0
            now = time()
            summary.debug("--- %s:%d (%s:%d) %s", self.lan_address[0], self.lan_address[1], self.wan_address[0], self.wan_address[1], self.connection_type)
            summary.debug("walk-attempt %d; success %d; invalid %d; boot-attempt %d; boot-success %d; reset %d",
                          self._statistics.walk_attempt,
                          self._statistics.walk_success,
                          self._statistics.walk_invalid_response_identifier,
                          self._statistics.walk_bootstrap_attempt,
                          self._statistics.walk_bootstrap_success,
                          self._statistics.walk_reset)
            summary.debug("walk-advice-out-request %d; in-response %d; in-new %d; in-request %d; out-response %d",
                          self._statistics.walk_advice_outgoing_request,
                          self._statistics.walk_advice_incoming_response,
                          self._statistics.walk_advice_incoming_response_new,
                          self._statistics.walk_advice_incoming_request,
                          self._statistics.walk_advice_outgoing_response)

            for community in sorted(self._communities.itervalues(), key=lambda community: community.cid):
                if community.get_classification() == u"PreviewChannelCommunity":
                    continue

                categories = {u"walk": [], u"stumble": [], u"intro": [], None: []}
                for candidate in community.candidates.itervalues():
                    if isinstance(candidate, WalkCandidate):
                        categories[candidate.get_category(now)].append(candidate)

                summary.debug("--- %s %s ---", community.cid.encode("HEX"), community.get_classification())
                summary.debug("--- [%2d:%2d:%2d:%2d]", len(categories[u"walk"]), len(categories[u"stumble"]), len(categories[u"intro"]), len(self._bootstrap_candidates))

                for category, candidates in categories.iteritems():
                    aged = [(candidate.age(now, category), candidate) for candidate in candidates]
                    for age, candidate in sorted(aged):
                        summary.debug("%5.1fs %s%s%s %-7s %-13s %s",
                                      min(age, 999.0),
                                      "O" if candidate.get_category(now) is None else " ",
                                      "E" if candidate.is_eligible_for_walk(now) else " ",
                                      "B" if isinstance(candidate, BootstrapCandidate) else " ",
                                      category,
                                      candidate.connection_type,
                                      candidate)
