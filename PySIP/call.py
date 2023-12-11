import asyncio
import signal
from typing import Literal
import wave
import edge_tts
from .CustomCommuicate import CommWithPauses, NoPausesFound
from pydub import AudioSegment
import os

from .filters import SIPMessageType, SIPStatus, SipMessage, ConnectionType
from .client import Client, SipFilter
from enum import Enum
from .rtp import PayloadType, RTPClient, TransmitType
from . import _print_debug_info

__all__ = [
    'CallState',
    'CallStatus',
    'VOIP',
    'TTS'
]

class CallState(Enum):
    DAILING = "DIALING"
    RINGING = "RINGING"
    ANSWERED = "ANSWERED"
    ENDED = "ENDED"
    FAILED = "FAILED"

class CallStatus(Enum):
    REGISTERING = "REGISTERING"
    REREGISTERING = "REREGISTERING"
    INVITING = "INVITING"
    REINVITING = "REINVITING"
    REGISTERED = "REGISTERED"
    INVITED = "INVITED"
    FAILED = "FAILED"
    INACTIVE = "INACTIVE"

class VOIP:
    """
    Represents a VoIP call using SIP protocol.

    Args:
        username (str): SIP username.
        route (str): SIP server route.
        password (str, optional): Authentication password.
        device_id (str, optional): Calling device ID.
        tts (bool, optional): Enable Text-to-Speech.
        text (str, optional): TTS text.

    Methods:
        :meth:`on_message()`: Start listening for SIP messages.
        :meth:`signal_handler()`: Handle signals during the call.
        :meth:`call(callee: str | int)`: Initiate a call.

    Example:
        voip_call = VOIP(username='user', route='server:port', password='pass')
        voip_call.call('11234567890')
    """
    def __init__(
        self,
        username: str,
        route: str,
        *,
        connection_type: Literal['TCP', 'UDP', 'TLS', 'TLSv1'] = 'TCP',
        password: str=None,
        device_id: str =None,
        token: str =None
    ) -> None:

        self.username = username
        self.route = route
        self.server = route.split(":")[0]
        self.port = int(route.split(":")[1])
        self.connection_type = ConnectionType(connection_type)
        self.password = password
        self.device_id = device_id
        self.token = token
        self.call_state = CallState.DAILING
        self.status = CallStatus.INACTIVE
        self.flag = False
        self.callee = None
        self.rtp_session = None
        self.last_error = None
        self.received_bytes = False
        self.last_body = None

        self.client = Client(
            self.username,
            self.route,
            self.callee,
            self.connection_type,
            self.password,
            self.device_id,
            self.token
        )
        self.on_message()

    async def call(self, callee: str | int, audio_file: str = None, tts: bool = False,
        text: str = None, language: str = 'so-SO-UbaxNeural'):
        """
        Initiate a call with the provided number.

        Arguments:
                :args:`callee` The phone number or contact identifier to call.
                :args:`audio_file` If provided this wil be used and no audio ill be generated.
                :args:`tts` Whether to use auto-generated audio from text.
                :arg:`text` This is the text used to generate the TTS.
                :arg:`language` The language that will be used t generate the TTS.


        The :meth:`call` method initializes a call to the specified `callee` number or identifier.
        If `callee` is an integer or string, it is treated as a phone number.
        This method sets up signal handling and runs the main client loop using asyncio.

        Example:
            ```
            voip_call = VOIP(*args, **kwargs)
            voip_call.call("1234567890", tts=True, text="Hello this is a test call")
            ```

        """
        self.callee = callee

        if isinstance(callee, int):
            self.callee = str(callee)

        self.tts = tts
        self.text = text
        self.language = language
        self.audio_file = audio_file

        self.client.callee = self.callee
        signal.signal(signal.SIGINT, self.signal_handler)

        if asyncio.get_event_loop().is_running():
            await asyncio.create_task(self.client.main(), name='pysip_1')
        else:
            asyncio.run(self.client.main())

    def on_message(self):
        @self.client.on_message()
        async def request_handler(msg: SipMessage):
            if not self.flag:
                return

            if msg.status in [SIPStatus.RINGING, SIPStatus.SESSION_PROGRESS]:
                if msg.body: # Pre-set the body in-case the serve doesn't send body everytime
                    self.last_body = msg.body

                if self.client.dialog_id is None:
                    self.client.dialog_id = msg.did

                self.client.on_call_tags["From"] = msg.from_tag

                self.client.on_call_tags['To'] = msg.to_tag
                self.client.on_call_tags["CSeq"] = msg.cseq
                self.client.on_call_tags["RSeq"] = msg.rseq

                prack = self.client.prack_generator()
                await self.client.send(prack)
                await self.make_call(msg)

            elif msg.get_header('Reason'):
                print('Callee hanged-up')
                self.last_error = "Callee hanged-up"
                if self.rtp_session:
                    self.received_bytes = self.bytes_to_audio(self.rtp_session.pmin.buffer)
                await self.client.hangup(self.rtp_session)

        @self.client.on_message(filters=SipFilter.RESPONSE)
        async def error_handler(message: SipMessage):
            if not message.status:
                return

            if message.method in ['PRACK', 'ACK']:
                    return

            if str(message.status).startswith('4') and message.status != SIPStatus.UNAUTHORIZED:
                """
                Handling client-side errors with status code 4xx
                """
                _print_debug_info('Client-side error, ending the call...')
                print('Error: ', message.status.description)
                self.last_error = str(message.status)
                await self.client.hangup(self.rtp_session)

            elif str(message.status).startswith('5'):
                """
                Handling server-side errors with status code 5xx
                """
                _print_debug_info('Server-side error, ending the call...')
                print('Error: ', message.status.description)
                self.last_error = str(message.status)
                await self.client.hangup(self.rtp_session)

            elif str(message.status).startswith('6'):
                """
                Handling Global errors with status code 6xx
                """
                _print_debug_info('Global error, ending the call...')
                print('Error: ', message.status.description)
                self.last_error = str(message.status)
                await self.client.hangup(self.rtp_session)

        @self.client.on_message(filters=SipFilter.REGISTER)
        async def handle_register(message: SipMessage):
            if message.type == SIPMessageType.MESSAGE:
                if message.get_header("Authorization"):
                    self.status = CallStatus.REREGISTERING
                    _print_debug_info("RE-REGISTERING...")
                else:
                    self.status = CallStatus.REGISTERING
                    _print_debug_info("REGISTERING...")

            elif message.status == SIPStatus.OK:
                self.status = CallStatus.REGISTERED
                _print_debug_info("REGISTERED...")

        @self.client.on_message(filters=SipFilter.INVITE)
        async def handle_invite(message: SipMessage):
            if message.type == SIPMessageType.MESSAGE:
                if message.get_header("Authorization"):
                    self.status = CallStatus.REINVITING
                    _print_debug_info("RE-INVITING...")
                else:
                    self.status = CallStatus.REGISTERING
                    _print_debug_info("INVITING...")

            elif message.status == SIPStatus.OK:
                self.status = CallStatus.INVITED
                self.call_state = CallState.ANSWERED
                ack = self.client.ack_call_answered()

                await self.client.send(ack)
                _print_debug_info("INVITED...")
                print("CALL HAS BEEN ANSWERED..")

            if self.status == CallStatus.REINVITING and message.status != \
            SIPStatus.TRYING and message.type == SIPMessageType.RESPONSE:
                # If this statement happens then it will pass all the
                # responses that are not :attr:`SIPSatatus.trying` which
                # can help us handle the events that occur after we send
                # the re-invite with authoriation
                _print_debug_info("This event occured: ", message.status)
                self.flag = True
                if message.body: # Pre-set the body in-case the serve doesn't send body everytime
                    self.last_body = message.body

    async def make_call(self, message: SipMessage):
        if self.call_state != CallState.DAILING:
            return
        self.call_state = CallState.RINGING

        body = self.last_body
        if message.body:
            body = message.body

        sdp = SipMessage.parse_sdp(body)
        rtp_session = RTPClient(sdp.rtpmap, self.client.my_private_ip, 64417,
                                    sdp.ip_address, sdp.port, TransmitType.SENDRECV)
        self.rtp_session = rtp_session
        rtp_session.start()
        asyncio.create_task(self.audio_writer(rtp_session), name='pysip_3')

    async def audio_writer(self, session: RTPClient):
        while self.call_state != CallState.ANSWERED:
            await asyncio.sleep(0.1)  # Introduce a small delay

        await asyncio.sleep(0.03)
        audio_file = self.audio_file
        if self.tts:
            tts = TTS(self.text, self.language, 'tts.mp3')
            audio_file = await tts.generate_audio()

        session.send_now(audio_file)

        sleep_time = self.get_audio_duration(audio_file)
        await asyncio.sleep(sleep_time + 4)
        self.received_bytes = self.bytes_to_audio(session.pmin.buffer)
        os.remove('recorded.wav')
        self.last_error = "Call ended"
        await self.client.hangup(session)

        await asyncio.sleep(1)

    def bytes_to_audio(self, buffer):
        with wave.open('recorded.wav', 'wb') as file:
            file.setnchannels(1) # mono
            file.setsampwidth(1)
            file.setframerate(8000)
            file.writeframes(buffer.read())

            # wav to mp3
            audio: AudioSegment = AudioSegment.from_wav('recorded.wav')
            audio.set_sample_width(2)
            audio.export('recorded.mp3')

            return True

    @classmethod
    def audio_duration(cls, audio_file_path):
        """
        works with any audio format
        """
        try:
            audio = AudioSegment.from_file(audio_file_path)
            duration_ms = len(audio)
            duration_seconds = duration_ms / 1000
            return duration_seconds
        except Exception as e:
            print("Error:", e)
            return None

    def get_audio_duration(self, file_path: str) -> float:
        # wav format only
        with wave.open(file_path, 'rb') as wav_file:
            sample_rate = wav_file.getframerate()
            num_frames = wav_file.getnframes()
            duration = num_frames / sample_rate
            return duration

    def signal_handler(self, sig, frame):
        print("\nCtrl+C detected. Sending CANCEL request and exiting...")
        asyncio.create_task(self.client.hangup(self.rtp_session), name='pysip_4')

class TTS:
    def __init__(
        self,
        text: str,
        voice: str,
        output_filename: str
    ) -> None:

        self.text = text
        self.voice = voice
        self.output_filename = output_filename

    async def generate_audio(self) -> str:
        try:
            communicate = CommWithPauses(self.text, self.voice)
            await communicate.save(self.output_filename)
        except NoPausesFound:
            communicate = edge_tts.Communicate(self.text, self.voice)
            await communicate.save(self.output_filename)

        file_name = self.convert_to_wav()
        self.cleanup()

        return file_name

    def convert_to_wav(self):
        sound: AudioSegment = AudioSegment.from_mp3(self.output_filename)
        wav_filename = os.path.splitext(self.output_filename)[0] + ".wav"
        sound.export(wav_filename, format='wav')

        return wav_filename

    def cleanup(self):
        os.remove(self.output_filename)





