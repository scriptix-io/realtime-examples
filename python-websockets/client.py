## Example Async Websocket client

## Run with (Dutch) BNR Radio as example
#
#  curl -s  http://icecast-bnr.cdp.triple-it.nl/bnr_mp3_96_06 |\
#       ffmpeg -loglevel panic -i - -ac 1 -acodec pcm_s16le -ar 16000 -f wav - |\
#       python3.7 client.py nl-nl token


import websockets
import asyncio
import argparse
from concurrent.futures.thread import ThreadPoolExecutor


async def writer(
    args: argparse.Namespace, websocket: websockets.WebSocketClientProtocol
) -> None:
    """Reads data from input file, and writes it to websocket.

    This function used a threadpool in order to async read the data in blocks of 32000 bytes. 
    As the read function is blocking we need to run it in a thread."""

    loop = asyncio.get_event_loop()
    threadpool = ThreadPoolExecutor(1)

    fp = open(args.input_stream, "rb")
    while True:
        data = await loop.run_in_executor(threadpool, fp.read, 32768)
        if not data:
            print("EOF")
            break
        await websocket.send(data)
        await asyncio.sleep(0.25)

    # Notify we are done
    await websocket.send('{"action": "stop"}')

    threadpool.shutdown()
    fp.close()


async def reader(websocket):
    async for message in websocket:
        print(message)

        # Stop when stopped
        if message == '{"state": "stopped"}' or message == '{"state":"stopped"}':
            print("Stopped")
            break


async def run_connection(args: argparse.Namespace) -> None:

    # Connect to websocket
    websocket: websockets.WebSocketClientProtocol = await websockets.connect(
        f"wss://api.zoommedia.ai/realtime?language={args.language}",
        extra_headers={"x-zoom-s2t-key": args.token},
    )

    # Initiate action start
    await websocket.send('{"action": "start"}')

    # Wait for listening event
    first_message = await websocket.recv()

    if isinstance(first_message, str) and '"listening"' in first_message:
        print("Server is listening")
        print(first_message)

        # Start async reader and writer
        task_reader = asyncio.create_task(reader(websocket))
        task_writer = asyncio.create_task(writer(args, websocket))

        # Wait for all tasks to finish
        w = await asyncio.wait(
            [task_writer, task_reader], return_when=asyncio.FIRST_EXCEPTION
        )

        if w[0] == task_reader:
            print("Reader finished")
            await task_writer
    else:
        print("Server not listening")
        print(first_message)

    # Close websocket
    await websocket.close()


def main():

    parser = argparse.ArgumentParser("Realtime Example Client")

    parser.add_argument("language", help="API Language")
    parser.add_argument("token", help="API Token")
    parser.add_argument(
        "input_stream",
        help="Input stream (default: /dev/stdin)",
        default="/dev/stdin",
        nargs="?",
    )

    args = parser.parse_args()

    # Initiate event loop
    loop = asyncio.get_event_loop()

    # Start loop
    loop.run_until_complete(run_connection(args))


if __name__ == "__main__":
    main()
