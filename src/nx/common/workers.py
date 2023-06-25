import hashlib
import logging
import os
import sys

from abc import ABC, abstractmethod
from signal import signal, SIGHUP

from scapy.all import ARP, Ether, IFACES, IP, sniff
from scapy.arch.linux import IFF_LOOPBACK

from .audit import audited_open, is_valid_secret
from .logging import init_logging
from ..services import Redis

log = logging.getLogger(__name__)

Unset = object()


class RestartWorker(Exception):
    pass


class Worker(ABC):
    @property
    @abstractmethod
    def WORKER_NAME(self):
        raise NotImplementedError

    def load_secret(self, key: str):
        value = self._getenv_secret(key)
        if is_valid_secret(value):
            return value
        value = self._readfile_secret(key)
        if is_valid_secret(value):
            return value
        raise KeyError(key)

    def _getenv_secret(self, key: str):
        key = f"{self.WORKER_NAME}_{key.replace('.', '_')}".upper()
        return os.getenv(key)

    def _readfile_secret(self, key: str):
        nx_confdir = os.getenv("CONFIGURATION_DIRECTORY")
        if not nx_confdir:
            return None
        return audited_open(
            os.path.join(nx_confdir, self.WORKER_NAME.lower(), key),
        ).read()

    @abstractmethod
    def run(self):
        log.info(f"Hi, I'm {self.WORKER_NAME}")
        self._cmd = [sys.executable] + sys.argv
        self._waiting_to_restart = False
        signal(SIGHUP, self._handle_SIGHUP)

    def _handle_SIGHUP(self, signum, _):
        if self._waiting_to_restart:
            log.info("received second SIGHUP, forcing restart now")
            self._checkpoint_worker()
        log.info("received SIGHUP, will restart at next checkpoint")
        self._waiting_to_restart = True

    def _checkpoint_worker(self):
        if not self._waiting_to_restart:
            return
        log.info("going down for restart")
        raise RestartWorker

    @classmethod
    def main(cls):
        init_logging()
        worker = cls()
        try:
            worker.run()
        except RestartWorker:
            command = worker._cmd
            log.info("restarting NOW...")
            sys.stdout.flush()
            sys.stderr.flush()
            os.execv(command[0], command)


class RedisClientWorker(Worker):
    def __init__(self, db=None):
        if db is None:
            db = Redis()
        self.db = db
        self.name = self.WORKER_NAME.lower()


class PacketSnifferWorker(RedisClientWorker):
    @property
    def interfaces(self):
        """An iterable of network interfaces to operate on."""
        return (dev
                for dev in IFACES.values()
                if (dev.is_valid()
                    and not dev.flags & IFF_LOOPBACK))

    @property
    @abstractmethod
    def WANTED_PACKETS(self):
        """The BPF filter to apply."""
        raise NotImplementedError

    @abstractmethod
    def process_packet(self, packet):
        """Function to apply to each packet. If something is returned,
        it is displayed."""
        raise NotImplementedError

    def run(self):
        super().run()

        filter = self.WANTED_PACKETS
        log.info(f"monitoring packets matching: {filter}")

        interfaces = [dev.name for dev in self.interfaces]
        log.info(f"listening on: {', '.join(sorted(interfaces))}")

        return sniff(
            prn=self._wrapped_process_packet,
            filter=filter,
            store=False,
            iface=interfaces)

    def _wrapped_process_packet(self, packet):
        try:
            return self._process_packet(packet)
        except:  # noqa: E722
            log.error("packet processing failed:", exc_info=True)
        finally:
            self._checkpoint_worker()

    # XXX refactor tests, then remove this method
    def _process_packet(self, packet):
        return PacketProcessor(self, packet).run()


class PacketProcessor:
    def __init__(self, worker, packet):
        self.worker = worker
        self.packet = packet

        self._issue_categories = []

        self._ether_layer = Unset
        self.packet_hash = None
        self.src_mac = None

    def run(self):
        heartbeat = self.worker.name, self.packet.time
        self.pipeline = self.worker.db.pipeline(transaction=False)
        try:
            try:
                try:
                    self._run()
                finally:
                    for set in self._issue_categories:
                        self.pipeline.sadd(set, self.packet_hash)
            finally:
                self.pipeline.hset("heartbeats", *heartbeat)
        finally:
            self.pipeline.execute()

    def _run(self):
        self.common_fields = {
            "last_seen": self.packet.time,
            "last_seen_by": self.worker.name,
            f"last_seen_by_{self.worker.name}": self.packet.time,
        }

        # Log the raw packet.
        if self.ether_layer is None:
            self.record_issue("first_layer")

        self.packet_hash = self._packet_hash()
        self.packet_key = f"pkt_{self.packet_hash}"

        if self.ether_layer is not None:
            self.src_mac = self.ether_layer.src.lower()
            self.mac_key = f"mac_{self.src_mac}"

        self._record_raw_packet()

        if self.ether_layer is None:
            return

        # Log the device sighting.
        self._record_device_sighting()

        # Hand over to worker-specific code.
        return self.worker.process_packet(
            packet=self.packet,
            pipeline=self.pipeline,
            common_fields=self.common_fields,
            macaddr=self.src_mac,
            mac_key=self.mac_key,
            packet_hash=self.packet_hash,
            packet_key=self.packet_key,
        )

    @property
    def ether_layer(self):
        if self._ether_layer is Unset:
            self._ether_layer = self.packet.getlayer(Ether)
        return self._ether_layer

    def _packet_hash(self):
        return hashlib.blake2s(self._packet_hash_bytes()).hexdigest()

    def _packet_hash_bytes(self):
        packet_bytes = self.packet.original
        if self.ether_layer is None:
            return packet_bytes

        copy_packet = self.ether_layer.__class__(packet_bytes)
        ipv4_layer = copy_packet.getlayer(IP)
        if ipv4_layer is None:
            if ARP not in copy_packet:
                self.record_issue("ether_payload")
            return packet_bytes

        ipv4_layer.id = 0xdead
        ipv4_layer.chksum = 0xbeef

        return bytes(copy_packet)

    def _record_raw_packet(self):
        fields = self.common_fields.copy()
        fields["raw_bytes"] = self.packet.original
        fields["last_sniffed_on"] = self.packet.sniffed_on
        if self.src_mac is not None:
            fields["last_seen_from"] = self.src_mac

        key, pipeline = self.packet_key, self.pipeline

        pipeline.hset(key, mapping=fields)
        pipeline.hsetnx(key, "first_seen", self.packet.time)
        pipeline.hincrby(key, "num_sightings", 1)

    def _record_device_sighting(self):
        pipeline = self.pipeline

        pipeline.sadd("macs", self.src_mac)

        key = self.mac_key
        pipeline.hset(key, mapping=self.common_fields)
        pipeline.hsetnx(key, "first_seen", self.packet.time)

        key = f"macpkts_{self.src_mac}"
        pipeline.hset(key, self.packet_hash, self.packet.time)

    def record_issue(self, category):
        """Note that there was an issue processing this packet."""
        self._issue_categories.append(f"unhandled:pkts:{category}")
