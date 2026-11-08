"""Minimal local SMTP sink: accepts and discards mail, so the app's magic-link mail step succeeds. Listens 0.0.0.0:2525."""
import socketserver


class H(socketserver.StreamRequestHandler):
    def handle(self):
        w = lambda s: self.wfile.write((s + "\r\n").encode())
        w("220 sink ESMTP")
        data = False
        for raw in self.rfile:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if data:
                if line == ".":
                    data = False
                    w("250 queued")
                continue
            u = line.upper()
            if u.startswith(("EHLO", "HELO")):
                w("250 sink")
            elif u.startswith("DATA"):
                data = True
                w("354 go")
            elif u.startswith("QUIT"):
                w("221 bye")
                return
            else:
                w("250 ok")


class S(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


S(("0.0.0.0", 2525), H).serve_forever()
