"""Radarr factice pour les tests de bout en bout (version 50) : répond à l'API v3 et journalise chaque requête.

Usage : python faux_radarr.py PORT FICHIER_JOURNAL. Clé attendue : CLE.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

LOG = sys.argv[2]
FILMS = [
    {"id": 21, "title": "Delta", "year": 2004, "tmdbId": 4001},
    {"id": 22, "title": "Charlie", "year": 2003, "tmdbId": 4002},
    {"id": 23, "title": "Alpha", "year": 2001, "tmdbId": 4003},
]


class Gestionnaire(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _journal(self):
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"m": self.command, "path": self.path}) + "\n")

    def _envoie(self, code, corps):
        data = json.dumps(corps).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._journal()
        if self.headers.get("X-Api-Key") != "CLE":
            return self._envoie(401, {"message": "Unauthorized"})
        chemin = urlparse(self.path).path
        if chemin == "/api/v3/system/status":
            return self._envoie(200, {"appName": "Radarr", "version": "5.9.9"})
        if chemin == "/api/v3/movie":
            return self._envoie(200, FILMS)
        self._envoie(404, {})

    def do_DELETE(self):
        self._journal()
        if self.headers.get("X-Api-Key") != "CLE":
            return self._envoie(401, {})
        requete = {k: v for k, v in parse_qs(urlparse(self.path).query).items()}
        self._envoie(200, {"deleted": True, "query": requete})


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1])), Gestionnaire).serve_forever()
