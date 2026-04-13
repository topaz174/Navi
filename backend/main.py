import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import websockets

from backend.engine import NaviEngine
from backend.voice import VoiceRecorder
from shared.constants import WS_EVENTS, WS_PORT

logging.basicConfig(
    level=logging.DEBUG,
    format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("navi")


class NaviServer:
    def __init__(self):
        self._engine: NaviEngine | None = None
        self._ws: websockets.WebSocketServerProtocol | None = None
        self._voice = VoiceRecorder()

    async def ws_send(self, event: str, payload: dict):
        if self._ws is None:
            return
        try:
            msg = json.dumps({"event": event, "payload": payload})
            await self._ws.send(msg)
        except Exception:
            pass

    async def handler(self, websocket):
        logger.info("Electron connected")
        self._ws = websocket
        self._engine = NaviEngine(self.ws_send)

        try:
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("Invalid JSON from Electron: %s", raw[:100])
                    continue

                event = msg.get("event")
                payload = msg.get("payload", {})

                if event == WS_EVENTS["dpr"]:
                    self._engine.set_dpr(
                        payload.get("scaleFactor", 2.0),
                        payload.get("logicalWidth", 0),
                        payload.get("logicalHeight", 0),
                        payload.get("workAreaY", 0),
                    )

                elif event == WS_EVENTS["goal"]:
                    text = payload.get("text", "").strip()
                    if text:
                        asyncio.ensure_future(self._engine.handle_goal(text))

                elif event == WS_EVENTS["cancel"]:
                    await self._engine.handle_cancel()

                elif event == WS_EVENTS["user_confirmed_done"]:
                    await self._engine.handle_user_confirmed_done()

                elif event == WS_EVENTS["user_continue"]:
                    asyncio.ensure_future(self._engine.handle_user_continue())

                elif event == WS_EVENTS["voice_start"]:
                    loop = asyncio.get_running_loop()
                    self._voice.start(
                        on_done=lambda text: asyncio.run_coroutine_threadsafe(
                            self.ws_send(WS_EVENTS["voice_transcript"], {"text": text}), loop
                        ),
                        on_error=lambda msg: asyncio.run_coroutine_threadsafe(
                            self.ws_send(WS_EVENTS["voice_error"], {"message": msg}), loop
                        ),
                    )

                elif event == WS_EVENTS["voice_stop"]:
                    self._voice.stop()

                else:
                    logger.debug("Unknown event from Electron: %s", event)

        except websockets.ConnectionClosed:
            logger.info("Electron disconnected")
        finally:
            self._ws = None
            if self._engine:
                await self._engine.handle_cancel()

    async def start(self):
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            logger.error("ANTHROPIC_API_KEY not set in .env — please add it and restart")
        else:
            logger.info("API key loaded")

        try:
            from backend.screenshot import capture_screenshot
            from PIL import Image
            import io
            import numpy as np
            png, w, h = capture_screenshot()
            arr = np.array(Image.open(io.BytesIO(png)))
            if arr.mean() < 1.0:
                logger.warning("Screenshot appears black — Screen Recording permission may be needed")
            else:
                logger.info("Screenshot capture OK (%dx%d)", w, h)
        except Exception as e:
            logger.warning("Screenshot test failed: %s", e)

        logger.info("Starting WebSocket server on port %d", WS_PORT)
        async with websockets.serve(self.handler, "localhost", WS_PORT):
            await asyncio.Future()


def main():
    server = NaviServer()
    asyncio.run(server.start())


if __name__ == "__main__":
    main()
