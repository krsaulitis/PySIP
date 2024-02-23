import asyncio
from enum import Enum
import logging
import random
from typing import Optional

from PySIP.exceptions import AudioStreamError, NoSupportedCodecsFound
from .audio_stream import AudioStream
from .udp_handler import open_udp_connection
from .utils.logger import logger
from .filters import PayloadType


SUPPORTED_CODEC = [PayloadType.PCMU, PayloadType.PCMA]


class RTPProtocol(Enum):
    UDP = "udp"
    AVP = "RTP/AVP"
    SAVP = "RTP/SAVP"


class TransmitType(Enum):
    RECVONLY = "recvonly"
    SENDRECV = "sendrecv"
    SENDONLY = "sendonly"
    INACTIVE = "inactive"

    def __str__(self):
        return self.value


class RTPClient:
    def __init__(
        self, offered_codecs, src_ip, src_port, dst_ip, dst_port, transmit_type
    ):
        self.offered_codecs = offered_codecs
        self.src_ip = src_ip
        self.src_port = src_port
        self.dst_ip = dst_ip
        self.dst_port = dst_port
        self.transmit_type = transmit_type
        self.selected_codec = self.select_audio_codecs(offered_codecs)
        self.udp_reader, self.udp_writter = None, None
        self.send_lock = asyncio.Lock()
        self.rtp_packet = RtpPacket(self.selected_codec)
        self.is_running = asyncio.Event()
        self._input_queue = asyncio.Queue()
        self._output_queue = asyncio.Queue()
        self._audio_stream: Optional[AudioStream] = None

    async def start(self):
        self.is_running.set()
        self.udp_reader, self.udp_writer = await open_udp_connection(
            (self.src_ip, self.src_port), (self.dst_port, self.dst_port)
        )

    def select_audio_codecs(self, offered_codecs):
        for codec in offered_codecs.values():
            if codec in SUPPORTED_CODEC:
                return codec

        raise NoSupportedCodecsFound

    def is_rfc_2833_supported(self, offered_codecs):
        for codec in offered_codecs.values():
            if codec == PayloadType.EVENT:
                return True

        return False

    async def send(self):
        if self.get_audio_stream:
            logger.log(
                logging.DEBUG, f"Started to send from steam source with id: {self.get_audio_stream.stream_id}"
            )
        else:
            logger.log(logging.WARNING, "No stream to send from")
            raise AudioStreamError
    async def _handle_rfc_2833(self, packet):
        dtmf_mapping = [
        "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "#", "A", "B", "C", "D"
        ]
        payload = packet.payload
        event = dtmf_mapping[payload[0]]

        if not packet.marker:
            return

        # check for registered callbacks
        if not self.__callbacks:
            logger.log(logging.DEBUG, "No callbacks passed to RtpHandler.")
            return
        if not (callbacks := self.__callbacks.get('dtmf_callback')):
            return
        # notify the callbacks
        for cb in callbacks:
            await cb(event)

        while True:
            start_processing = asyncio.get_event_loop().time()
            payload = await self.get_audio_stream.input_q.get()
            # if all frames are sent then break
            if not payload:
                logger.log(
                    logging.DEBUG, f"Sent all frames from source with id: {source}."
                )
                self.get_audio_stream.audio_sent_future.set_result("Sent all frames")
                break

            packet = self.rtp_packet.generate_packet(payload)
            await self.udp_writer.write(packet)

            delay = (1 / self.selected_codec.rate) * 160
            processing_time = asyncio.get_event_loop().time() - start_processing
            sleep_time = max(0, delay - processing_time) / 1.75

            await asyncio.sleep(sleep_time)

    async def receive(self):
        while True:
            if not self.is_running.is_set():
                break

            if not self.udp_reader:
                logger.log(logging.CRITICAL, "There is no UdpReader, can't read!")
                return
            try:
                data = await asyncio.wait_for(self.udp_reader.read(4096), 0.5)
            except asyncio.TimeoutError:
                continue  # this is neccesary to avoid blocking of checking
                # whether app is runing or not
    async def frame_monitor(self):
        # first add stream queue to the output _output_queues
        self._output_queues['frame_monitor'] = stream_q = asyncio.Queue()
        self._output_queues['new'] = asyncio.Queue()
        while True:
            await asyncio.sleep(0.01)
            if not self.is_running.is_set():
                break
            if not self.__callbacks:
                logger.log(logging.DEBUG, "No callbacks passed to RtpHandler.")
                break
            try:
                frame = stream_q.get_nowait()
                if not (callbacks := self.__callbacks.get('frame_monitor')):
                    break
                # check for registered callbacks
                for cb in callbacks:
                    await cb(frame)

            except asyncio.QueueEmpty:
                continue 

    @property
    def get_audio_stream(self):
        return self._audio_stream

    def set_audio_stream(self, stream: AudioStream):
        self._audio_stream = stream


class RtpPacket:
    def __init__(self, selected_codec):
        self.selected_codec = selected_codec
        self.out_sequence = random.randint(200, 800)
        self.out_timestamp = random.randint(500, 5000)
        self.out_ssrc = random.randint(1000, 65530)

    def generate_packet(self, payload):
        packet = b"\x80"
        packet += chr(int(self.selected_codec)).encode("utf8")
        packet += self.get_header(payload)
        packet += payload
        return packet

    def get_header(self, payload):
        seq = self.out_sequence.to_bytes(2, byteorder="big")
        ts = self.out_timestamp.to_bytes(4, byteorder="big")

        ssrc = self.out_ssrc.to_bytes(4, byteorder="big")
        header = seq + ts + ssrc
        self.out_sequence = (self.out_sequence + 1) % 65535  # Wrap around at 2^16 - 1
        self.out_timestamp = (
            self.out_timestamp + len(payload)
        ) % 4294967295  # Wrap around at 2^32 -1
        return header
