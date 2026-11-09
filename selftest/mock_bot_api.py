# -*- coding: utf-8 -*-
"""Локальный мок Telegram Bot API для сквозной проверки tg_ref.py."""
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

STATE = {
    "links": {},      # invite_link -> {"chat_id","name","join_count"}
    "members": {"-100111": 1000, "-100222": 500},
    "sent": [],
    "counter": 0,
    "updates": [],
    "files": {},      # file_id -> bytes
    "description": "",
}
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, result):
        body = json.dumps({"ok": True, "result": result}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # скачивание файла: /file/bot<token>/<path>
        if self.path.startswith("/file/"):
            fid = self.path.rsplit("/", 1)[-1]
            with LOCK:
                blob = STATE["files"].get(fid)
            if blob is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
            return
        self.send_response(404)
        self.end_headers()

    def _handle_multipart(self, raw: bytes, ctype: str):
        """Принимает файл (multipart) и сохраняет его содержимое."""
        bm = re.search(r"boundary=([^;]+)", ctype)
        boundary = ("--" + bm.group(1)) if bm else None
        fields = {}
        file_id = "mockfile1"
        blob = b""
        if boundary:
            parts = raw.split(boundary.encode())
            for part in parts:
                if b"filename=" not in part:
                    continue
                head, _, body = part.partition(b"\r\n\r\n")
                blob = body.rsplit(b"\r\n", 1)[0]
                fname = re.search(rb'filename="([^"]+)"', head)
                with LOCK:
                    STATE["files"][file_id] = blob
                    STATE["sent"].append({"method": "sendDocument", "file_name":
                                          (fname.group(1).decode() if fname else ""),
                                          "size": len(blob)})
        return self._send({"message_id": 1,
                           "document": {"file_id": file_id,
                                        "file_name": "tg_ref_state.json.gz"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "")

        if "multipart/form-data" in ctype:
            return self._handle_multipart(raw, ctype)

        payload = json.loads(raw or b"{}")
        m = re.search(r"/bot[^/]+/(\w+)$", self.path)
        method = m.group(1) if m else ""
        with LOCK:
            if method == "getMe":
                return self._send({"id": 42, "username": "test_ref_bot", "first_name": "Test",
                                   "description": STATE["description"]})
            if method == "setMyDescription":
                STATE["description"] = payload.get("description", "")
                return self._send(True)
            if method == "getChat":
                cid = str(payload.get("chat_id"))
                if cid not in STATE["members"]:
                    return self._send({"id": cid, "type": "channel", "title": f"Канал {cid}",
                                       "username": f"chan{cid[-3:]}"})
                return self._send({"id": cid, "type": "channel", "title": f"Канал {cid}",
                                   "username": f"chan{cid[-3:]}"})
            if method == "getChatMember":
                return self._send({"status": "administrator", "can_invite_users": True})
            if method == "getChatMemberCount":
                return self._send(STATE["members"].get(str(payload.get("chat_id")), 0))
            if method == "createChatInviteLink":
                STATE["counter"] += 1
                link = f"https://t.me/+mocklink{STATE['counter']}"
                STATE["links"][link] = {"chat_id": str(payload.get("chat_id")),
                                        "name": payload.get("name"), "join_count": 0}
                return self._send({"invite_link": link, "name": payload.get("name"),
                                   "join_count": 0, "invite_count": 0, "join_request_count": 0})
            if method == "getChatInviteLinkStats":
                link = payload.get("invite_link")
                if link not in STATE["links"]:
                    return self._send({"ok": False, "description": "CHAT_INVITE_LINK_INVALID"})
                return self._send({"invite_link": link, "join_count": STATE["links"][link]["join_count"],
                                   "invite_count": 3, "join_request_count": 0})
            if method == "sendMessage":
                STATE["sent"].append(payload)
                return self._send({"message_id": len(STATE["sent"])})
            if method == "sendDocument":
                STATE["sent"].append(payload)
                return self._send({"message_id": len(STATE["sent"])})
            if method == "getUpdates":
                off = int(payload.get("offset") or 0)
                ups = [u for u in STATE["updates"] if int(u.get("update_id", 0)) >= off]
                STATE["updates"] = []
                if not ups:
                    time.sleep(0.5)   # чтобы бот не крутил цикл впустую
                return self._send(ups)
            if method == "__push_update":
                upd = payload.get("update") or {}
                upd.setdefault("update_id", STATE["counter"] + 1)
                STATE["counter"] = int(upd["update_id"])
                STATE["updates"].append(upd)
                return self._send({"queued": len(STATE["updates"])})
            if method == "getFile":
                fid = payload.get("file_id")
                return self._send({"file_id": fid, "file_path": f"mock/{fid}"})
            if method == "__sent":
                return self._send(STATE["sent"])
            if method == "__dump":
                return self._send({"updates": STATE["updates"], "counter": STATE["counter"],
                                   "sent": len(STATE["sent"])})
            if method == "__add_joins":
                for link in STATE["links"].values():
                    link["join_count"] += int(payload.get("n", 0))
                for cid in list(STATE["members"]):
                    STATE["members"][cid] += int(payload.get("n", 0))
                return self._send({"links": len(STATE["links"])})
        return self._send(True)


if __name__ == "__main__":
    srv = HTTPServer(("127.0.0.1", 8765), Handler)
    print("mock up on 8765", flush=True)
    srv.serve_forever()
