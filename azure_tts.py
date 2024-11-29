# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE.md file in the project root for full license information.

import asyncio
import logging
import os
from typing import AsyncIterator, Tuple
import traceback
import azure.cognitiveservices.speech as speechsdk
import numpy as np
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from dotenv import load_dotenv
load_dotenv()
logger = logging.getLogger(__name__)

def calculate_energy(frame_data):
    # Convert the byte data to a numpy array for easier processing (assuming 16-bit PCM)
    data = np.frombuffer(frame_data, dtype=np.int16)
    # Calculate the energy as the sum of squares of the samples
    energy = np.sum(data**2) / len(data)
    return energy

class AioStream:
    def __init__(self):
        self._queue = asyncio.Queue()

    def write_data(self, data: bytes):
        self._queue.put_nowait(data)

    def end_of_stream(self):
        self._queue.put_nowait(None)

    async def read(self) -> bytes:
        chunk = await self._queue.get()
        if chunk is None:
            raise StopAsyncIteration
        return chunk

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self):
        return await self.read()

class Client:
    def __init__(self, synthesis_pool_size: int = 2):
        if synthesis_pool_size < 1:
            raise ValueError("synthesis_pool_size must be at least 1")
        self.synthesis_pool_size = synthesis_pool_size
        self._counter = 0
        self.voice = None

    def configure(self, voice: str):
        logger.info(f"Configuring voice: {voice}")
        self.voice = "hi-IN-AnanyaNeural"

        self.speech_config = speechsdk.SpeechConfig(
            endpoint=f"wss://{os.environ['AZURE_SPEECH_REGION']}.tts.speech.microsoft.com/cognitiveservices/websocket/v2",
            subscription=os.environ["AZURE_SPEECH_KEY"]
        )
        self.speech_config.speech_synthesis_voice_name = voice
        self.speech_config.set_speech_synthesis_output_format(speechsdk.SpeechSynthesisOutputFormat.Raw16Khz16BitMonoPcm)
        self.speech_synthesizers = [speechsdk.SpeechSynthesizer(speech_config=self.speech_config, audio_config=None) for _ in range(self.synthesis_pool_size)]
        for s in self.speech_synthesizers:
            s.synthesis_started.connect(lambda evt: logger.info(f"Synthesis started: {evt.result.reason}"))
            s.synthesis_completed.connect(lambda evt: logger.info(f"Synthesis completed: {evt.result.reason}"))
            s.synthesis_canceled.connect(lambda evt: logger.error(f"Synthesis canceled: {evt.result.reason}"))

    def text_to_speech(self, voice: str, speed: str = "medium") -> Tuple[speechsdk.SpeechSynthesisRequest.InputStream, AioStream]:
        # input_stream = sp
        logger.info(f"Synthesizing text with voice: {voice}")
        self.configure(voice)
        synthesis_request = speechsdk.SpeechSynthesisRequest(
            input_type=speechsdk.SpeechSynthesisRequestInputType.TextStream)
        # synthesis_request.rate = speed
        self._counter = (self._counter + 1) % len(self.speech_synthesizers)
        current_synthesizer = self.speech_synthesizers[self._counter]

        result = current_synthesizer.start_speaking(synthesis_request)
        stream = speechsdk.AudioDataStream(result)
        aio_stream = AioStream()
        async def read_from_data_stream():
            leading_silence_skipped = False
            silence_detection_frames_size = int(50 * 16000 * 2 / 1000)  # 50 ms
            loop = asyncio.get_running_loop()
            while True:
                if not leading_silence_skipped:
                    if stream.position >= 3 * silence_detection_frames_size:
                        leading_silence_skipped = True
                        continue
                    frame_data = bytes(silence_detection_frames_size)
                    lenx = await loop.run_in_executor(None, stream.read_data, frame_data)
                    if lenx == 0:
                        if stream.status != speechsdk.StreamStatus.AllData:
                            logger.error(f"Speech synthesis failed: {stream.status}, details: {stream.cancellation_details.error_details}")
                        break
                    energy = await loop.run_in_executor(None, calculate_energy, frame_data)
                    if energy < 500:
                        logger.info("Silence detected, skipping")
                        continue
                    leading_silence_skipped = True
                    stream.position = stream.position - silence_detection_frames_size
                chunk = bytes(1600*2)
                read = await loop.run_in_executor(None, stream.read_data, chunk)
                if read == 0:
                    break
                print("chunk", len(chunk))
                aio_stream.write_data(chunk[:read])
            if stream.status != speechsdk.StreamStatus.AllData:
                logger.error(f"Speech synthesis failed: {stream.status}, details: {stream.cancellation_details.error_details}")
            aio_stream.end_of_stream()

        asyncio.create_task(read_from_data_stream())
        return synthesis_request.input_stream, aio_stream
    
    
    async def text_to_speech_realtime_old(self, text: str, voice: str, speed: str = "medium"):
        self._counter = (self._counter + 1) % len(self.speech_synthesizers)
        current_synthesizer = self.speech_synthesizers[self._counter]
        current_synthesizer.properties.set_property(speechsdk.PropertyId.SpeechServiceConnection_SynthVoice, voice)
        ssml = f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='hi-IN'><voice name='{voice}'><prosody rate='{speed}'>{text}</prosody></voice></speak>"
        print(ssml)
        result = current_synthesizer.start_speaking_ssml(ssml)
        stream = speechsdk.AudioDataStream(result)
        first = True
        leading_silence_skipped = False
        silence_detection_frames_size = int(50 * 16000 * 2 / 1000)  # 50 ms
        while True:
            if not leading_silence_skipped:
                if stream.position >= 3 * silence_detection_frames_size:
                    leading_silence_skipped = True
                    continue
                frame_data = bytes(silence_detection_frames_size)
                _ = stream.read_data(frame_data)
                energy = calculate_energy(frame_data)
                if energy < 500:
                    continue
                leading_silence_skipped = True
                stream.position = stream.position - silence_detection_frames_size
            chunk = bytes(1600*2)  # 200 ms duration
            read = stream.read_data(chunk)
            if read == 0:
                break
            yield chunk[:read]
        if stream.status != speechsdk.StreamStatus.AllData:
            logging.error(f"Speech synthesis failed: {stream.status}, details: {stream.cancellation_details.error_details}")
            return  
        
    @classmethod
    async def text_to_speech_realtime(self, text: str, voice: str, speed: str = "medium"):
        # Azure Speech Service Configuration
        speech_config = speechsdk.SpeechConfig(subscription=os.environ['AZURE_SPEECH_KEY'], region=os.environ['AZURE_SPEECH_REGION'])
        speech_config.speech_synthesis_voice_name = voice
        speech_config.set_speech_synthesis_output_format(speechsdk.SpeechSynthesisOutputFormat.Raw24Khz16BitMonoPcm)
        speech_synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=None)
        # Synthesize speech
        ssml = f'<speak xmlns="http://www.w3.org/2001/10/synthesis" xmlns:mstts="http://www.w3.org/2001/mstts" xmlns:emo="http://www.w3.org/2009/10/emotionml" version="1.0" xml:lang="hi-IN"><voice name="{voice}">{text}</voice></speak>'
        #result = speech_synthesizer.speak_text_async(text).get()
        result = speech_synthesizer.speak_ssml_async(ssml).get()
        if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
            print("Speech synthesized successfully.")
            audio_data = result.audio_data
            return audio_data
        else:
            print("Failed to synthesize speech:", result.reason)
        

if __name__ == "__main__":
    async def main():
        logging.basicConfig(level=logging.INFO)
        client = Client()
        print("client", client)
        input, output = client.text_to_speech("en-US-Andrew:DragonHDLatestNeural")

        async def read_output():
            audio = b''
            async for chunk in output:
                print(len(chunk))
                audio += chunk
            with open("output.wav", "wb") as f:
                f.write(b'RIFF')
                f.write((36 + len(audio)).to_bytes(4, 'little'))
                f.write(b'WAVE')
                f.write(b'fmt ')
                f.write((16).to_bytes(4, 'little'))
                f.write((1).to_bytes(2, 'little'))
                f.write((1).to_bytes(2, 'little'))
                f.write((24000).to_bytes(4, 'little'))
                f.write((48000).to_bytes(4, 'little'))
                f.write((2).to_bytes(2, 'little'))
                f.write((16).to_bytes(2, 'little'))
                f.write(b'data')
                f.write((len(audio)).to_bytes(4, 'little'))
                f.write(audio)
        async def put_input():
            for c in ['Hello,', ' world!', 'My', 'name','is', 'Manoranjan','How Can i help you today', '。']:
                input.write(c)
            input.close()
                # await asyncio.sleep(1)
        await asyncio.gather(read_output(), put_input())
        # add header to the wave file

    asyncio.run(main())