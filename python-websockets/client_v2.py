import subprocess

import websockets
import asyncio

STREAM_URL = "https://stream.bnr.nl/bnr_mp3_128_20"  ## Your streaming URL
LANGUAGE = "nl"  ## "nl" for Dutch, "en" for English, .... you can use this link for more languages: https://apidocs.scriptix.io/models/overview/
API_KEY = "YOUR_API_KEY"  ## Your API key, you can get it from https://scriptix.app/settings/api (you need to be logged in),
# generate a new token of type realtime


async def writer(websocket: websockets.WebSocketClientProtocol) -> None:
    """
    Runs ffmpeg to capture audio from the stream and sends the processed audio to the websocket.

    :param websocket: WebSocket connection to the transcription server.
    """

    # Command to run ffprobe
    command = [
        "ffmpeg",
        "-loglevel",
        "panic",
        "-i",
        STREAM_URL,
        "-ac",
        "1",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-f",
        "wav",
        "-",
    ]

    # Run ffprobe as a subprocess
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    while True:
        data = await asyncio.to_thread(process.stdout.read, 1024)  # Read smaller chunks
        if not data:
            print("EOF or no data")
            break
        await websocket.send(data)

    # Notify we are done
    await websocket.send('{"action": "stop"}')

    # Close the process
    process.stdout.close()
    process.terminate()
    process.wait()


async def reader(websocket: websockets.WebSocketClientProtocol) -> None:
    """
    Reads and prints messages from the WebSocket server.

    :param websocket: WebSocket connection to the transcription server.
    """
    async for message in websocket:
        print(message)
        if '"state":"stopped"' in message:
            print("Stopped")
            break


async def run_connection() -> None:
    """
    Establishes a WebSocket connection to the transcription server and manages the streaming process.
    """
    async with websockets.connect(
        f"wss://realtime.scriptix.io/v2/realtime?language={LANGUAGE}",
        extra_headers={"x-zoom-s2t-key": API_KEY},
    ) as websocket:

        await websocket.send('{"action": "start"}')
        first_message = await websocket.recv()

        if '"listening"' in first_message:
            print("Server is listening")
            print(first_message)

            await asyncio.gather(reader(websocket), writer(websocket))
        else:
            print("Server not listening")
            print(first_message)


def main():
    """
    Main function to start the asynchronous WebSocket connection.
    """
    asyncio.run(run_connection())


# Entry point check
if __name__ == "__main__":
    main()
