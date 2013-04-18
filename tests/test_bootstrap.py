from unittest import skip
from time import time
from socket import getfqdn

from ..candidate import BootstrapCandidate
from ..message import Message, DropMessage
from ..dprint import dprint
from .debugcommunity.community import DebugCommunity
from .dispersytestclass import DispersyTestClass, call_on_dispersy_thread

class TestBootstrapServers(DispersyTestClass):
    @skip("The stress test is not actually a unittest")
    @call_on_dispersy_thread
    def test_servers_are_up(self):
        """
        Sends a dispersy-introduction-request to the trackers and counts how long it takes until the
        dispersy-introduction-response is received.
        """
        class PingCommunity(DebugCommunity):
            def __init__(self, *args, **kargs):
                # original walker callbacks (will be set during super(...).__init__)
                self._original_on_introduction_response = None

                super(PingCommunity, self).__init__(*args, **kargs)

                self._request = {}
                self._summary = {}
                self._hostname = {}
                self._identifiers = {}
                self._pcandidates = self._dispersy._bootstrap_candidates.values()
                # self._pcandidates = [BootstrapCandidate(("130.161.211.198", 6431))]

                for candidate in self._pcandidates:
                    self._request[candidate.sock_addr] = {}
                    self._summary[candidate.sock_addr] = []
                    self._hostname[candidate.sock_addr] = getfqdn(candidate.sock_addr[0])
                    self._identifiers[candidate.sock_addr] = ""

            def _initialize_meta_messages(self):
                super(PingCommunity, self)._initialize_meta_messages()

                # replace the callbacks for the dispersy-introduction-response message
                meta = self._meta_messages[u"dispersy-introduction-response"]
                self._original_on_introduction_response = meta.handle_callback
                self._meta_messages[meta.name] = Message(meta.community, meta.name, meta.authentication, meta.resolution, meta.distribution, meta.destination, meta.payload, meta.check_callback, self.on_introduction_response, meta.undo_callback, meta.batch)

            @property
            def dispersy_enable_candidate_walker(self):
                return False

            @property
            def dispersy_enable_candidate_walker_responses(self):
                return True

            def dispersy_take_step(self):
                test.fail("we disabled the walker")

            def on_introduction_response(self, messages):
                now = time()
                dprint("PONG")
                for message in messages:
                    candidate = message.candidate
                    if candidate.sock_addr in self._request:
                        request_stamp = self._request[candidate.sock_addr].pop(message.payload.identifier, 0.0)
                        self._summary[candidate.sock_addr].append(now - request_stamp)
                        self._identifiers[candidate.sock_addr] = message.authentication.member.mid
                return self._original_on_introduction_response(messages)

            def ping(self, now):
                dprint("PING", line=1)
                for candidate in self._pcandidates:
                    request = self._dispersy.create_introduction_request(self, candidate, False)
                    self._request[candidate.sock_addr][request.payload.identifier] = now

            def summary(self):
                for sock_addr, rtts in sorted(self._summary.iteritems()):
                    if rtts:
                        dprint(self._identifiers[sock_addr].encode("HEX"), " %15s:%-5d %-30s " % (sock_addr[0], sock_addr[1], self._hostname[sock_addr]), len(rtts), "x  ", round(sum(rtts) / len(rtts), 1), " avg  [", ", ".join(str(round(rtt, 1)) for rtt in rtts[-10:]), "]", force=True)
                    else:
                        dprint(sock_addr[0], ":", sock_addr[1], "  missing", force=True)

            def finish(self, request_count, min_response_count, max_rtt):
                for sock_addr, rtts in self._summary.iteritems():
                    test.assertLess(min_response_count, len(rtts), "Only received %d/%d responses from %s:%d" % (len(rtts), request_count, sock_addr[0], sock_addr[1]))
                    test.assertLess(sum(rtts) / len(rtts), max_rtt, "Average RTT %f from %s:%d is more than allowed %f" % (sum(rtts) / len(rtts), sock_addr[0], sock_addr[1], max_rtt))


        community = PingCommunity.create_community(self._dispersy, self._my_member)

        test = self
        PING_COUNT = 10
        ASSERT_MARGIN = 0.9
        MAX_RTT = 0.5
        for _ in xrange(PING_COUNT):
            community.ping(time())
            yield 5.0
            community.summary()

        # cleanup
        community.create_dispersy_destroy_community(u"hard-kill")
        self._dispersy.get_community(community.cid).unload_community()

        # assert when not all of the servers are responding
        community.finish(PING_COUNT, PING_COUNT * ASSERT_MARGIN, MAX_RTT)

    @skip("The stress test is not actually a unittest")
    @call_on_dispersy_thread
    def test_perform_heavy_stress_test(self):
        """
        Sends many a dispersy-introduction-request messages to a single tracker and counts how long
        it takes until the dispersy-introduction-response messages are received.
        """
        class PingCommunity(DebugCommunity):
            def __init__(self, master, candidates):
                super(PingCommunity, self).__init__(master)

                self._original_my_member = self._my_member

                self._request = {}
                self._summary = {}
                self._hostname = {}
                self._identifiers = {}
                self._pcandidates = candidates
                self._queue = []
                # self._pcandidates = self._dispersy._bootstrap_candidates.values()
                # self._pcandidates = [BootstrapCandidate(("130.161.211.198", 6431))]

                for candidate in self._pcandidates:
                    self._request[candidate.sock_addr] = {}
                    self._summary[candidate.sock_addr] = []
                    self._hostname[candidate.sock_addr] = getfqdn(candidate.sock_addr[0])
                    self._identifiers[candidate.sock_addr] = ""

            def _initialize_meta_messages(self):
                super(PingCommunity, self)._initialize_meta_messages()

                # replace the callbacks for the dispersy-introduction-response message
                meta = self._meta_messages[u"dispersy-introduction-response"]
                self._meta_messages[meta.name] = Message(meta.community, meta.name, meta.authentication, meta.resolution, meta.distribution, meta.destination, meta.payload, self.check_introduction_response, meta.handle_callback, meta.undo_callback, meta.batch)

            @property
            def dispersy_enable_candidate_walker(self):
                return False

            @property
            def dispersy_enable_candidate_walker_responses(self):
                return True

            def dispersy_take_step(self):
                test.fail("we disabled the walker")

            def create_dispersy_identity(self, sign_with_master=False, store=True, update=True, member=None):
                self._my_member = member if member else self._original_my_member
                try:
                    return super(PingCommunity, self).create_dispersy_identity(sign_with_master, store, update)
                finally:
                    self._my_member = self._original_my_member

            def check_introduction_response(self, messages):
                now = time()
                for message in messages:
                    candidate = message.candidate
                    if candidate.sock_addr in self._request:
                        request_stamp = self._request[candidate.sock_addr].pop(message.payload.identifier, 0.0)
                        if request_stamp:
                            self._summary[candidate.sock_addr].append(now - request_stamp)
                            self._identifiers[candidate.sock_addr] = message.authentication.member.mid
                        else:
                            dprint("identifier clash ", message.payload.identifier, level="warning")

                    yield DropMessage(message, "not doing anything in this script")

            def prepare_ping(self, member):
                self._my_member = member
                try:
                    for candidate in self._pcandidates:
                        request = self._dispersy.create_introduction_request(self, candidate, False, forward=False)
                        self._queue.append((request.payload.identifier, request.packet, candidate))
                finally:
                    self._my_member = self._original_my_member

            def ping_from_queue(self, count):
                for identifier, packet, candidate in self._queue[:count]:
                    self._dispersy.endpoint.send([candidate], [packet])
                    self._request[candidate.sock_addr][identifier] = time()

                self._queue = self._queue[count:]

            def ping(self, member):
                self._my_member = member
                try:
                    for candidate in self._pcandidates:
                        request = self._dispersy.create_introduction_request(self, candidate, False)
                        self._request[candidate.sock_addr][request.payload.identifier] = time()
                finally:
                    self._my_member = self._original_my_member

            def summary(self):
                for sock_addr, rtts in sorted(self._summary.iteritems()):
                    if rtts:
                        dprint(self._identifiers[sock_addr].encode("HEX"), " %15s:%-5d %-30s " % (sock_addr[0], sock_addr[1], self._hostname[sock_addr]), len(rtts), "x  ", round(sum(rtts) / len(rtts), 1), " avg  [", ", ".join(str(round(rtt, 1)) for rtt in rtts[-10:]), "]", force=True)
                    else:
                        dprint(sock_addr[0], ":", sock_addr[1], "  missing", force=True)

        MEMBERS = 10000 # must be a multiple of 100
        COMMUNITIES = 1
        ROUNDS = 10

        dprint("prepare communities, members, etc", force=1)
        with self._dispersy.database:
            candidates = [BootstrapCandidate(("130.161.211.245", 6429), False)]
            communities = [PingCommunity.create_community(self._dispersy, self._my_member, candidates) for _ in xrange(COMMUNITIES)]
            members = [self._dispersy.get_new_member(u"low") for _ in xrange(MEMBERS)]

            for community in communities:
                for member in members:
                    community.create_dispersy_identity(member=member)

        dprint("prepare request messages", force=1)
        for _ in xrange(ROUNDS):
            for community in communities:
                for member in members:
                    community.prepare_ping(member)

            yield 5.0
        yield 15.0

        dprint("ping-ping", force=1)
        BEGIN = time()
        for _ in xrange(ROUNDS):
            for community in communities:
                for _ in xrange(MEMBERS/100):
                    community.ping_from_queue(100)
                    yield 0.1

            for community in communities:
                community.summary()
        END = time()

        yield 10.0
        dprint("--- did ", ROUNDS * MEMBERS, " requests per community", force=1)
        dprint("--- spread over ", round(END - BEGIN, 1), " seconds", force=1)
        for community in communities:
            community.summary()

        # cleanup
        community.create_dispersy_destroy_community(u"hard-kill")
        self._dispersy.get_community(community.cid).unload_community()