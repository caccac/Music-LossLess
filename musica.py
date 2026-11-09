#!/usr/bin/env python3
"""
Music Lossless — app Música no Mac.

1. Troca faixas "Enviadas", "Correspondidas" ou "Indisponíveis" pela versão do
   catálogo Apple Music (Lossless / Dolby Atmos) e troca álbuns do Apple Music
   sem Atmos por uma edição com Atmos, quando existe.
2. Remove músicas repetidas (mesma faixa, mesmo artista, mesmo álbum, duração
   igual), mantendo a de melhor qualidade.

Em toda troca: a contagem de reproduções, a nota, o "favorito" e a posição nas
playlists passam para a faixa que fica. As antigas que são seus arquivos
(enviadas/correspondidas) são baixadas e copiadas para
~/Music/Backup versões antigas antes de saírem da biblioteca.

Uso:  python3 musica.py     → abre http://localhost:8765
Só usa a biblioteca padrão do Python.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
DADOS = BASE / "dados"
CONFIG = DADOS / "config.json"
TOKEN = DADOS / "token_usuario.json"
ANALISE = DADOS / "analise.json"
CACHE = DADOS / "cache_catalogo.json"
REGISTRO = DADOS / "registro.jsonl"
SNAPSHOTS = DADOS / "playlists_antes"
PREFS = DADOS / "preferencias.json"
IDIOMA = {"atual": "pt"}  # "pt" ou "en"; a página escolhe


def tr(pt, en):
    return en if IDIOMA["atual"] == "en" else pt


def pasta_backup():
    # Se já existe backup em um dos idiomas, continua nele (trocar de idioma não divide os backups).
    for nome in ("Backup versões antigas", "Old versions backup"):
        if (Path.home() / "Music" / nome).is_dir():
            return Path.home() / "Music" / nome
    return Path.home() / "Music" / tr("Backup versões antigas", "Old versions backup")


def nome_playlist_backup():
    return tr("zz Backup antes da troca (automático)", "zz Backup before swap (automatic)")
PORTA = 8765
API = "https://api.music.apple.com"
DADOS.mkdir(exist_ok=True)

STATUS_TROCAR = {"uploaded", "matched", "no longer available", "unknown"}
STATUS_BACKUP = {"uploaded", "matched", "purchased", "unknown"}  # arquivos que são seus
RANK_STATUS = {"subscription": 5, "purchased": 4, "matched": 3, "uploaded": 2,
               "unknown": 1, "no longer available": 0}
CAMPOS = ["databaseID", "persistentID", "name", "artist", "albumArtist", "album",
          "discNumber", "trackNumber", "duration", "cloudStatus", "kind", "bitRate",
          "sampleRate", "playedCount", "playedDate", "rating", "favorited", "disliked",
          "dateAdded", "mediaKind"]


# ───────────────────────── tarefa em segundo plano ─────────────────────────

class Tarefa:
    def __init__(self):
        self.lock = threading.Lock()
        self.rodando = False
        self.nome = ""
        self.etapa = ""
        self.feito = 0
        self.total = 0
        self.erro = None
        self.log = []

    def iniciar(self, nome, fn, *args):
        with self.lock:
            if self.rodando:
                raise RuntimeError(tr("Já existe uma tarefa em andamento.", "A task is already running."))
            self.rodando, self.nome, self.etapa = True, nome, ""
            self.feito = self.total = 0
            self.erro = None

        def alvo():
            try:
                fn(*args)
                self.msg(tr("Concluído.", "Done."))
            except Exception as e:  # noqa: BLE001
                self.erro = str(e)
                self.msg(tr("ERRO: ", "ERROR: ") + str(e))
                traceback.print_exc()
            finally:
                self.rodando = False
        threading.Thread(target=alvo, daemon=True).start()

    def msg(self, texto):
        linha = time.strftime("%H:%M:%S  ") + texto
        print(linha, flush=True)
        self.log.append(linha)
        del self.log[:-400]

    def passo(self, etapa, total=0):
        self.etapa, self.total, self.feito = etapa, total, 0
        self.msg(etapa)

    def estado(self):
        return {"rodando": self.rodando, "nome": self.nome, "etapa": self.etapa,
                "feito": self.feito, "total": self.total, "erro": self.erro,
                "log": self.log[-120:]}


T = Tarefa()


def carregar(p, padrao=None):
    try:
        return json.loads(Path(p).read_text("utf-8"))
    except (OSError, ValueError):
        return padrao


def salvar(p, dados, privado=False):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False), "utf-8")
    if privado:
        os.chmod(tmp, 0o600)
    tmp.replace(p)


def em_lotes(lista, n):
    for i in range(0, len(lista), n):
        yield lista[i:i + n]


# ───────────────────────── app Música (AppleScript / JXA) ─────────────────────────

def osa(script, args=(), js=False, timeout=3600):
    cmd = ["osascript"] + (["-l", "JavaScript"] if js else []) + ["-"] + [str(a) for a in args]
    r = subprocess.run(cmd, input=script, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(tr("app Música: ", "Music app: ") + r.stderr.strip()[-400:])
    return r.stdout.strip()


JS_EXPORTAR = r"""
function run(argv) {
  const M = Application("Music");
  const lib = M.libraryPlaylists[0];
  const t = lib.tracks;
  const campos = %s;
  const o = {};
  for (const c of campos) { try { o[c] = t[c](); } catch (e) { o[c] = null; } }
  for (const c of ["playedDate", "dateAdded"])
    if (o[c]) o[c] = o[c].map(d => (d ? d.toISOString() : null));
  o.arquivos = {};
  try {
    const f = lib.fileTracks, ids = f.persistentID(), loc = f.location();
    ids.forEach((id, i) => { if (loc[i]) o.arquivos[id] = loc[i].toString(); });
  } catch (e) {}
  return JSON.stringify(o);
}
""" % json.dumps(CAMPOS)

JS_ARQUIVOS = r"""
function run(argv) {
  const o = {};
  try {
    const f = Application("Music").libraryPlaylists[0].fileTracks;
    const ids = f.persistentID(), loc = f.location();
    ids.forEach((id, i) => { if (loc[i]) o[id] = loc[i].toString(); });
  } catch (e) {}
  return JSON.stringify(o);
}
"""

JS_PLAYLISTS = r"""
function run(argv) {
  const M = Application("Music");
  const ps = M.userPlaylists;
  const pid = ps.persistentID(), nome = ps.name(), smart = ps.smart(), tipo = ps.specialKind();
  const r = [];
  for (let i = 0; i < pid.length; i++) {
    if (smart[i] || tipo[i] !== "none") continue;
    let ids = [];
    try { ids = ps[i].tracks.persistentID(); } catch (e) {}
    r.push({ pid: pid[i], nome: nome[i], faixas: ids });
  }
  return JSON.stringify(r);
}
"""

JS_METADADOS = r"""
function run(argv) {
  const ops = JSON.parse(argv[0]);
  const lib = Application("Music").libraryPlaylists[0];
  const r = [];
  for (const o of ops) {
    try {
      const t = lib.tracks.whose({ persistentID: o.id })[0];
      if (o.playedCount != null) t.playedCount = o.playedCount;
      if (o.playedDate) t.playedDate = new Date(o.playedDate);
      if (o.rating != null) t.rating = o.rating;
      if (o.favorited) t.favorited = true;
      if (o.disliked) t.disliked = true;
      r.push([o.id, "ok"]);
    } catch (e) { r.push([o.id, String(e)]); }
  }
  return JSON.stringify(r);
}
"""

AS_TROCAR_NA_PLAYLIST = r"""
on run argv
	set pid to item 1 of argv
	set AppleScript's text item delimiters to ","
	set velhas to text items of (item 2 of argv)
	set novas to text items of (item 3 of argv)
	tell application "Music"
		set lib to library playlist 1
		set p to first user playlist whose persistent ID is pid
		set falhas to 0
		repeat with n in novas
			if (contents of n) is not "" then
				if not (exists (some track of p whose persistent ID is (contents of n))) then
					try
						duplicate (first track of lib whose persistent ID is (contents of n)) to p
					on error
						set falhas to falhas + 1
					end try
				end if
			end if
		end repeat
		repeat with o in velhas
			if (contents of o) is not "" then
				try
					delete (every track of p whose persistent ID is (contents of o))
				end try
			end if
		end repeat
		return "ok:" & falhas
	end tell
end run
"""

AS_PLAYLIST_BACKUP = r"""
on run argv
	set nome to item 1 of argv
	set AppleScript's text item delimiters to ","
	set ids to text items of (item 2 of argv)
	tell application "Music"
		set lib to library playlist 1
		if exists user playlist nome then delete user playlist nome
		set p to make new user playlist with properties {name:nome}
		repeat with i in ids
			try
				duplicate (first track of lib whose persistent ID is (contents of i)) to p
			end try
		end repeat
		download p
		return count of tracks of p
	end tell
end run
"""

AS_APAGAR_PLAYLIST = r"""
on run argv
	tell application "Music"
		if exists user playlist (item 1 of argv) then delete user playlist (item 1 of argv)
	end tell
end run
"""

AS_APAGAR = r"""
on run argv
	set AppleScript's text item delimiters to ","
	set ids to text items of (item 1 of argv)
	set falhas to {}
	tell application "Music"
		set lib to library playlist 1
		repeat with i in ids
			try
				delete (first track of lib whose persistent ID is (contents of i))
			on error
				set end of falhas to (contents of i)
			end try
		end repeat
	end tell
	return falhas as text
end run
"""


def exportar_biblioteca():
    o = json.loads(osa(JS_EXPORTAR, js=True))
    n = len(o.get("databaseID") or [])
    faixas = []
    for i in range(n):
        t = {c: (o[c][i] if o.get(c) else None) for c in CAMPOS}
        t["id"] = t["persistentID"]  # estável; o database ID muda quando o app renumera a biblioteca
        t["arquivo"] = o["arquivos"].get(t["id"])
        faixas.append(t)
    return faixas


def arquivos_locais():
    return json.loads(osa(JS_ARQUIVOS, js=True))


def e_musica(t):
    mk = (t.get("mediaKind") or "song").lower()
    return mk in ("song", "música", "musica") and "vídeo" not in (t.get("kind") or "").lower() \
        and "pdf" not in (t.get("kind") or "").lower()


# ───────────────────────── comparação de nomes ─────────────────────────

def sem_acento(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))


def norm(s):
    s = sem_acento(s).lower().replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


EXTRA = re.compile(r"remaster|deluxe|edi[cç][aã]o|edition|version|vers[aã]o|expanded|anniversary|"
                   r"anivers|bonus|legacy|special|collector|reissue|mono|stereo|explicit|clean|dolby|"
                   r"atmos|spatial|super|\b\d{4}\b|mix|edit|\bsingle\b|\bep\b", re.I)
MARCAS = {"ao vivo": r"\blive\b|ao vivo|en vivo|in concert", "acústico": r"acoustic|acustico|unplugged",
          "demo": r"\bdemo\b", "instrumental": r"instrumental", "karaokê": r"karaok",
          "remix": r"remix|\brmx\b", "a cappella": r"a capp?ella", "acelerada": r"sped up|slowed|nightcore",
          "tributo": r"tribute|tributo|as made famous|in the style of"}
RE_PAR = re.compile(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]")
RE_SUF = re.compile(r"\s[-–—]\s(.*)$")


def marcas(s):
    s = sem_acento(s or "")
    return frozenset(k for k, p in MARCAS.items() if re.search(p, s, re.I))


def base(s):
    s = RE_PAR.sub(lambda m: " " if EXTRA.search(m.group(1)) else m.group(0), s or "")
    m = RE_SUF.search(s)
    if m and EXTRA.search(m.group(1)):
        s = s[:m.start()]
    return norm(s)


def sim(a, b):
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    r = SequenceMatcher(None, a, b).ratio()
    curto, longo = sorted((a, b), key=len)
    if len(curto) >= 4 and (longo.startswith(curto + " ") or longo.endswith(" " + curto)):
        r = max(r, 0.9)
    return r


VARIOS = {"varios artistas", "various artists", "various", "varios", "artistas variados"}


def norm_artista(s):
    s = norm(s)
    return s[4:] if s.startswith("the ") else s


def sim_artista(a, b):
    a, b = norm_artista(a), norm_artista(b)
    if a in VARIOS and b in VARIOS:
        return 1.0
    r = sim(a, b)
    # "A, B and C" × "A"
    partes_a = set(re.split(r"\s(?:and|feat|ft|with|e|x)\s|,", a))
    partes_b = set(re.split(r"\s(?:and|feat|ft|with|e|x)\s|,", b))
    if {p.strip() for p in partes_a} & {p.strip() for p in partes_b} - {""}:
        r = max(r, 0.85)
    return r


# ───────────────────────── Apple Music API ─────────────────────────

def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def gerar_token(team, kid, chave, validade=60 * 60 * 24 * 30):
    agora = int(time.time())
    entrada = (b64u(json.dumps({"alg": "ES256", "kid": kid}).encode()) + "." +
               b64u(json.dumps({"iss": team, "iat": agora - 300, "exp": agora + validade}).encode()))
    openssl = shutil.which("openssl") or "/usr/bin/openssl"
    r = subprocess.run([openssl, "dgst", "-sha256", "-sign", os.path.expanduser(chave)],
                       input=entrada.encode(), capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(tr("Não consegui assinar com a chave .p8: ", "Could not sign with the .p8 key: ") + r.stderr.decode()[-300:])
    der = r.stdout  # SEQUENCE { INTEGER r, INTEGER s }
    i = 2 if der[1] < 0x80 else 2 + (der[1] & 0x7F)

    def inteiro(i):
        if der[i] != 2:
            raise RuntimeError(tr("Assinatura inesperada do openssl.", "Unexpected signature from openssl."))
        n = der[i + 1]
        return int.from_bytes(der[i + 2:i + 2 + n], "big"), i + 2 + n
    rr, i = inteiro(i)
    ss, _ = inteiro(i)
    return entrada + "." + b64u(rr.to_bytes(32, "big") + ss.to_bytes(32, "big"))


class AppleMusic:
    def __init__(self):
        self.dev = None
        self.user = None
        self.sf = "br"
        self.ultimo = 0.0
        self.intervalo = 0.25  # segundos entre requisições; cresce sozinho se a API reclamar
        self.tlock = threading.Lock()
        self.clock = threading.Lock()
        self.cache = carregar(CACHE, {})
        self.mudou = 0

    def salvar_cache(self, forcar=False):
        with self.clock:
            if self.mudou and (forcar or self.mudou >= 200):
                salvar(CACHE, self.cache)
                self.mudou = 0

    def req(self, metodo, caminho, params=None, usuario=False, cache_chave=None):
        if not self.dev:
            raise RuntimeError(tr("Configure a chave MusicKit primeiro.", "Set up the MusicKit key first."))
        if usuario and not self.user:
            raise RuntimeError(tr("Entre no Apple Music primeiro.", "Sign in to Apple Music first."))
        if cache_chave and cache_chave in self.cache:
            return self.cache[cache_chave]
        url = API + caminho + ("?" + urllib.parse.urlencode(params, safe=",[]") if params else "")
        recusas = 0
        for tentativa in range(8):
            with self.tlock:
                espera = self.ultimo + self.intervalo - time.time()
                if espera > 0:
                    time.sleep(espera)
                self.ultimo = time.time()
            cab = {"Authorization": "Bearer " + self.dev}
            if usuario:
                cab["Music-User-Token"] = self.user
            req = urllib.request.Request(url, method=metodo, headers=cab,
                                         data=b"" if metodo == "POST" else None)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    corpo = resp.read()
                dados = json.loads(corpo) if corpo else {}
                if cache_chave:
                    with self.clock:
                        self.cache[cache_chave] = dados
                        self.mudou += 1
                    self.salvar_cache()
                with self.tlock:  # voltando ao normal, acelera aos poucos
                    self.intervalo = max(0.25, self.intervalo * 0.97)
                return dados
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return {"data": []}
                # A API devolve 401 vazio de vez em quando (ritmo alto, chave recém-criada).
                # Trata como "espere e tente de novo"; só desiste se insistir.
                if e.code in (401, 403) and recusas < 5:
                    recusas += 1
                if e.code == 429 or e.code >= 500 or (e.code in (401, 403) and recusas < 5):
                    with self.tlock:
                        self.intervalo = min(2.0, self.intervalo * 1.5)
                    time.sleep(min(30, 1.5 * 2 ** tentativa))
                    continue
                if e.code in (401, 403):
                    raise RuntimeError(tr(f"Apple Music recusou o acesso ({e.code}) várias vezes seguidas. "
                                          "Confira a chave MusicKit ou entre de novo.",
                                          f"Apple Music refused access ({e.code}) several times in a row. "
                                          "Check the MusicKit key or sign in again.")) from e
                raise RuntimeError(f"Apple Music API {e.code}: {e.read()[:300]!r}") from e
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(min(60, 2 ** tentativa))
        raise RuntimeError(tr("Apple Music não respondeu: ", "Apple Music did not respond: ") + caminho)


AM = AppleMusic()


def buscar_albuns(termo, limite=12):
    termo = termo.strip()[:120]
    if not termo:
        return []
    d = AM.req("GET", f"/v1/catalog/{AM.sf}/search",
               {"term": termo, "types": "albums", "limit": str(limite)},
               cache_chave=f"{AM.sf}|ba|{termo.lower()}")
    return [a["id"] for a in d.get("results", {}).get("albums", {}).get("data", [])]


def buscar_albuns_por_musica(termo):
    termo = termo.strip()[:120]
    if not termo:
        return []
    d = AM.req("GET", f"/v1/catalog/{AM.sf}/search",
               {"term": termo, "types": "songs", "limit": "10"},
               cache_chave=f"{AM.sf}|bm|{termo.lower()}")
    ids = []
    for s in d.get("results", {}).get("songs", {}).get("data", []):
        m = re.search(r"/album/[^/]*/(\d+)", s.get("attributes", {}).get("url", ""))
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    return ids


def resumir_album(a):
    at = a.get("attributes", {}) or {}
    faixas = []
    for x in a.get("relationships", {}).get("tracks", {}).get("data", []):
        xa = x.get("attributes") or {}
        if x.get("type") != "songs" or not xa.get("playParams"):
            continue
        faixas.append({"id": x["id"], "nome": xa.get("name", ""), "disco": xa.get("discNumber") or 1,
                       "num": xa.get("trackNumber"), "dur": (xa.get("durationInMillis") or 0) / 1000})
    arte = (at.get("artwork") or {}).get("url", "").replace("{w}", "160").replace("{h}", "160")
    return {"id": a["id"], "nome": at.get("name", ""), "artista": at.get("artistName", ""),
            "url": at.get("url"), "variants": at.get("audioVariants") or [], "arte": arte,
            "ano": (at.get("releaseDate") or "")[:4], "clean": at.get("contentRating") == "clean",
            "streaming": bool(at.get("playParams")), "faixas": faixas}


def detalhes_albuns(ids):
    faltam = [i for i in ids if f"{AM.sf}|al|{i}" not in AM.cache]
    for lote in em_lotes(faltam, 15):
        d = AM.req("GET", f"/v1/catalog/{AM.sf}/albums",
                   {"ids": ",".join(lote), "include": "tracks", "extend": "audioVariants"})
        recebidos = {a["id"]: a for a in d.get("data", [])}
        with AM.clock:
            for i in lote:
                AM.cache[f"{AM.sf}|al|{i}"] = resumir_album(recebidos[i]) if i in recebidos else None
                AM.mudou += 1
        AM.salvar_cache()
    return [AM.cache[f"{AM.sf}|al|{i}"] for i in ids if AM.cache.get(f"{AM.sf}|al|{i}")]


def albuns_da_biblioteca():
    """(artista, álbum) da biblioteca → id do álbum no catálogo (só os que vêm do Apple Music)."""
    mapa, offset = {}, 0
    while True:
        d = AM.req("GET", "/v1/me/library/albums",
                   {"limit": "100", "offset": str(offset), "include": "catalog"}, usuario=True)
        for a in d.get("data", []):
            at = a.get("attributes", {}) or {}
            cat = (a.get("relationships", {}).get("catalog", {}) or {}).get("data") or []
            if cat:
                mapa[(norm_artista(at.get("artistName")), norm(at.get("name")))] = cat[0]["id"]
        T.msg(tr("   álbuns da biblioteca na nuvem: ", "   cloud library albums: ") + str(offset + len(d.get("data", []))))
        if not d.get("next"):
            break
        offset += 100
    return mapa


# ───────────────────────── casar biblioteca × catálogo ─────────────────────────

def casar_faixas(locais, catalogo):
    opcoes = []
    for li, l in enumerate(locais):
        bl, ml, dl = base(l["name"]), marcas(l["name"]), l.get("duration") or 0
        for ci, c in enumerate(catalogo):
            if marcas(c["nome"]) != ml:
                continue
            s = sim(bl, base(c["nome"]))
            if s < 0.82:
                continue
            dd = abs(dl - c["dur"]) if dl and c["dur"] else 0
            if dl and dd > max(10, 0.07 * dl):
                continue
            mesmo_lugar = l.get("trackNumber") == c["num"] and (l.get("discNumber") or 1) == c["disco"]
            opcoes.append((s - dd / 300 + (0.05 if mesmo_lugar else 0), li, ci))
    pares, usados = {}, set()
    for _, li, ci in sorted(opcoes, reverse=True):
        if li not in pares and ci not in usados:
            pares[li] = ci
            usados.add(ci)
    return pares


def avaliar(g, c):
    if not c or not c["streaming"] or not c["faixas"]:
        return None
    pares = casar_faixas(g["faixas"], c["faixas"])
    cob = len(pares) / len(g["faixas"])
    sa = sim_artista(g["artista"], c["artista"])
    sb = sim(base(g["album"]), base(c["nome"])) if g["album"] else 0.7
    if g["album"] and marcas(g["album"]) != marcas(c["nome"]):
        sb -= 0.4
    if cob == 1 and sb >= 0.85 and sa >= 0.85:
        conf = 3
    elif cob >= 0.8 and sb >= 0.6 and sa >= 0.7:
        conf = 2
    elif cob >= 0.5 and sa >= 0.6 and sb >= 0.4:
        conf = 1
    else:
        return None
    v = c["variants"]
    nota = cob * 0.6 + sb * 0.25 + sa * 0.15 - (0.05 if c["clean"] else 0)
    return {"conf": conf, "cob": cob, "pares": pares, "nota": nota,
            "ordem": (conf, "dolby-atmos" in v, "hi-res-lossless" in v, "lossless" in v, nota)}


def escolher(g, ids):
    melhor = None
    for c in detalhes_albuns(ids):
        a = avaliar(g, c)
        if a and (melhor is None or a["ordem"] > melhor[0]["ordem"]):
            melhor = (a, c)
    return melhor


def agrupar(faixas):
    grupos = defaultdict(list)
    for t in faixas:
        if not e_musica(t):
            continue
        st = t["cloudStatus"]
        if st in STATUS_TROCAR:
            tipo = "trocar"
        elif st == "subscription":
            tipo = "atmos"
        else:
            continue
        art = t["albumArtist"] or t["artist"] or ""
        chave = (tipo, norm_artista(art), norm(t["album"]) or "§" + norm(t["name"]))
        grupos[chave].append(t)
    saida = []
    for (tipo, _, _), ts in grupos.items():
        t0 = ts[0]
        art = t0["albumArtist"] or t0["artist"] or ""
        gid = hashlib.sha1(f"{tipo}|{norm_artista(art)}|{norm(t0['album'])}|{norm(t0['name']) if not t0['album'] else ''}"
                           .encode()).hexdigest()[:12]
        ts.sort(key=lambda t: ((t["discNumber"] or 1), (t["trackNumber"] or 0)))
        saida.append({"id": gid, "tipo": tipo, "artista": art, "album": t0["album"] or "", "faixas": ts})
    return saida


def processar_grupo(g, atual):
    if not (g["artista"] or g["album"] or any(t["name"] for t in g["faixas"])):
        return None
    termo_album = base(g["album"]) if g["album"] else norm(g["faixas"][0]["name"])
    ids = buscar_albuns(f'{g["artista"]} {termo_album}')
    melhor = escolher(g, ids[:8])
    if not melhor or melhor[0]["cob"] < 0.8:
        extra = []
        for t in g["faixas"][:2]:
            extra += buscar_albuns_por_musica(f'{t["artist"] or g["artista"]} {base(t["name"])}')
        if not ids and g["album"]:
            extra += buscar_albuns(termo_album)
        extra = [i for i in dict.fromkeys(extra) if i not in ids[:8]][:10]
        if extra:
            outro = escolher(g, extra)
            if outro and (not melhor or outro[0]["ordem"] > melhor[0]["ordem"]):
                melhor = outro
    if not melhor:
        return None
    a, c = melhor
    if g["tipo"] == "atmos":
        if "dolby-atmos" not in c["variants"] or a["conf"] < 2:
            return None
        cid = atual.get((norm_artista(g["artista"]), norm(g["album"])))
        if cid:
            if cid == c["id"]:
                return None
            cur = detalhes_albuns([cid])
            if cur and "dolby-atmos" in cur[0]["variants"]:
                return None
        elif norm(c["nome"]) == norm(g["album"]):
            return None  # provavelmente já é este álbum
    faixas = []
    for li, ci in sorted(a["pares"].items()):
        l, n = g["faixas"][li], c["faixas"][ci]
        faixas.append({"old": l["id"], "nome": l["name"], "artista": l["artist"], "albumAntigo": l["album"],
                       "status": l["cloudStatus"],
                       "dur": l["duration"], "new": n["id"], "novoNome": n["nome"], "novoDur": n["dur"]})
    sem_par = [g["faixas"][i]["name"] for i in range(len(g["faixas"])) if i not in a["pares"]]
    info = {k: c[k] for k in ("id", "nome", "artista", "url", "variants", "arte", "ano")}
    return {"id": g["id"], "tipo": g["tipo"], "artista": g["artista"], "album": g["album"] or tr("(sem álbum)", "(no album)"),
            "status": dict(Counter(t["cloudStatus"] for t in g["faixas"])), "nFaixas": len(g["faixas"]),
            "catalogo": info, "conf": ["", "baixa", "média", "alta"][a["conf"]],
            "cobertura": round(a["cob"], 3), "faixas": faixas, "semPar": sem_par}


def job_analisar():
    T.passo(tr("Lendo a biblioteca do app Música…", "Reading the Music library…"))
    faixas = exportar_biblioteca()
    grupos = agrupar(faixas)
    n_trocar = sum(g["tipo"] == "trocar" for g in grupos)
    T.msg(tr(f"{len(faixas)} faixas. {n_trocar} álbuns com faixas enviadas/correspondidas/indisponíveis; "
             f"{len(grupos) - n_trocar} álbuns do Apple Music para checar se existe edição com Atmos.",
             f"{len(faixas)} tracks. {n_trocar} albums with uploaded/matched/unavailable tracks; "
             f"{len(grupos) - n_trocar} Apple Music albums to check for a Dolby Atmos edition."))
    atual = {}
    if AM.user:
        T.passo(tr("Lendo os álbuns da biblioteca na nuvem…", "Reading the cloud library albums…"))
        try:
            atual = albuns_da_biblioteca()
        except Exception as e:  # noqa: BLE001
            T.msg(tr("   não consegui ler (sigo sem): ", "   could not read them (continuing without): ") + str(e))
    T.passo(tr("Procurando cada álbum no catálogo…", "Looking up each album in the catalog…"), len(grupos))
    itens, nao_achados, falhas = [], [], 0

    def um(g):
        try:
            return g, processar_grupo(g, atual), None
        except Exception as e:  # noqa: BLE001
            return g, None, e
    with ThreadPoolExecutor(2) as ex:
        for g, item, erro in ex.map(um, grupos):
            T.feito += 1
            if erro:
                falhas += 1
                T.msg(tr("   falhou: ", "   failed: ") + f"{g['artista']} — {g['album']}: {erro}")
                if falhas > 25 and falhas > T.feito / 2:
                    raise RuntimeError(tr("Muitas falhas seguidas na API; parei.", "Too many API failures in a row; stopped."))
            elif item:
                itens.append(item)
            elif g["tipo"] == "trocar":
                nao_achados.append({"artista": g["artista"], "album": g["album"] or g["faixas"][0]["name"],
                                    "status": dict(Counter(t["cloudStatus"] for t in g["faixas"])),
                                    "nFaixas": len(g["faixas"])})
            if T.feito % 50 == 0:
                T.msg(tr(f"   {T.feito}/{len(grupos)} álbuns — {len(itens)} com versão melhor",
                         f"   {T.feito}/{len(grupos)} albums — {len(itens)} with a better version"))
    AM.salvar_cache(True)
    feitos = {i["id"] for i in (carregar(ANALISE, {}) or {}).get("itens", []) if i.get("feito")}
    for i in itens:
        i["feito"] = i["id"] in feitos
    itens.sort(key=lambda i: (i["tipo"], norm(i["artista"]), norm(i["album"])))
    nao_achados.sort(key=lambda i: (norm(i["artista"]), norm(i["album"])))
    salvar(ANALISE, {"versao": VERSAO_ANALISE, "quando": datetime.now().isoformat(timespec="seconds"),
                     "storefront": AM.sf,
                     "itens": itens, "naoAchados": nao_achados, "falhas": falhas})
    T.msg(tr(f"Pronto: {len(itens)} álbuns com versão melhor; {len(nao_achados)} sem equivalente no catálogo.",
             f"Done: {len(itens)} albums with a better version; {len(nao_achados)} with no catalog match.")
          + (tr(f" {falhas} falharam — analise de novo para tentar só esses (o resto vem do cache).",
                f" {falhas} failed — analyze again to retry just those (the rest comes from the cache).")
             if falhas else ""))


# ───────────────────────── troca (comum a tudo) ─────────────────────────

def destino_backup(t, origem):
    seguro = lambda s: re.sub(r'[/:\\]', "-", (s or "Sem nome").strip())[:120] or "Sem nome"  # noqa: E731
    pasta = pasta_backup() / seguro(t["albumArtist"] or t["artist"]) / seguro(t["album"])
    pasta.mkdir(parents=True, exist_ok=True)
    alvo = pasta / Path(origem).name
    n = 2
    while alvo.exists() and alvo.stat().st_size != Path(origem).stat().st_size:
        alvo = pasta / f"{Path(origem).stem} ({n}){Path(origem).suffix}"
        n += 1
    return alvo


def fazer_backup(ids, por_id):
    """Baixa e copia as faixas para a pasta de backup. Devolve os ids que estão salvos com segurança."""
    if not ids:
        return set()
    locais = arquivos_locais()
    faltam = [i for i in ids if i not in locais]
    if faltam:
        T.msg(tr(f"Baixando {len(faltam)} faixas antigas para o backup…",
                 f"Downloading {len(faltam)} old tracks for the backup…"))
        nome_pl = nome_playlist_backup()
        osa(AS_PLAYLIST_BACKUP, [nome_pl, ",".join(map(str, faltam))])
        ultimo_avanco, antes = time.time(), -1
        limite_sem_avanco = 240
        while True:
            locais = arquivos_locais()
            prontos = sum(i in locais for i in faltam)
            if prontos == len(faltam):
                break
            if prontos != antes:
                antes, ultimo_avanco = prontos, time.time()
                T.msg(tr("   baixadas ", "   downloaded ") + f"{prontos}/{len(faltam)}")
            elif time.time() - ultimo_avanco > limite_sem_avanco:
                T.msg(tr(f"   {len(faltam) - prontos} não baixaram em {limite_sem_avanco // 60} min; "
                         "essas ficam na biblioteca.",
                         f"   {len(faltam) - prontos} did not download in {limite_sem_avanco // 60} min; "
                         "those stay in the library."))
                break
            time.sleep(10)
        osa(AS_APAGAR_PLAYLIST, [nome_pl])
    ok = set()
    for i in ids:
        origem = locais.get(i)
        if not origem or not Path(origem).is_file():
            continue
        alvo = destino_backup(por_id[i], origem)
        if not alvo.exists():
            shutil.copy2(origem, alvo)
        if alvo.stat().st_size == Path(origem).stat().st_size:
            ok.add(i)
    T.msg(tr(f"   backup: {len(ok)}/{len(ids)} faixas copiadas para {pasta_backup()}",
             f"   backup: {len(ok)}/{len(ids)} tracks copied to {pasta_backup()}"))
    return ok


MESCLADOS = DADOS / "mesclados.json"


def juntar_metadados(pares, por_id):
    """Passa reproduções, nota, favorito e "não gostei" das antigas para a nova.
    Cada par só é somado uma vez, mesmo que a troca seja repetida depois de uma interrupção."""
    feitos = set(carregar(MESCLADOS, []) or [])
    pares = [(o, n) for o, n in pares if f"{o}>{n}" not in feitos]
    antigos_por_novo = defaultdict(list)
    for o, n in pares:
        antigos_por_novo[n].append(por_id[o])
    ops = []
    for n, antigos in antigos_por_novo.items():
        nt = por_id.get(n) or {}
        op = {"id": n}
        soma = sum(a.get("playedCount") or 0 for a in antigos)
        if soma:
            op["playedCount"] = (nt.get("playedCount") or 0) + soma
        datas = [d for d in [nt.get("playedDate")] + [a.get("playedDate") for a in antigos] if d]
        if datas and max(datas) != nt.get("playedDate"):
            op["playedDate"] = max(datas)
        nota = max([a.get("rating") or 0 for a in antigos])
        if nota and not nt.get("rating"):
            op["rating"] = nota
        fav = any(a.get("favorited") for a in antigos)
        if fav and not nt.get("favorited"):
            op["favorited"] = True
        if any(a.get("disliked") for a in antigos) and not fav and not nt.get("favorited") \
                and not nt.get("disliked"):
            op["disliked"] = True
        if len(op) > 1:
            ops.append(op)
    erros = 0
    for lote in em_lotes(ops, 150):
        for _, r in json.loads(osa(JS_METADADOS, [json.dumps(lote)], js=True)):
            erros += r != "ok"
    feitos.update(f"{o}>{n}" for o, n in pares)
    salvar(MESCLADOS, sorted(feitos))
    T.msg(tr(f"   reproduções/notas/favoritos copiados para {len(ops) - erros} faixas",
             f"   play counts/ratings/favorites copied to {len(ops) - erros} tracks")
          + (tr(f" ({erros} falharam)", f" ({erros} failed)") if erros else ""))


def ler_playlists():
    return json.loads(osa(JS_PLAYLISTS, js=True))


def guardar_playlists(pls):
    salvar(SNAPSHOTS / f"playlists-{time.strftime('%Y%m%d-%H%M%S')}.json", pls)


def trocar_na_playlist(p, velhas, novas):
    """Edição mínima: a nova entra no fim da playlist e a antiga sai. Refazer o trecho final para manter
    a posição fazia o iCloud mesclar versões e triplicar a playlist."""
    if not velhas and not novas:
        return
    r = osa(AS_TROCAR_NA_PLAYLIST, [p["pid"], ",".join(velhas), ",".join(novas)])
    if r != "ok:0":
        T.msg(tr(f"   playlist “{p['nome']}”: {r.split(':')[-1]} faixas não entraram.",
                 f"   playlist “{p['nome']}”: {r.split(':')[-1]} tracks could not be added."))


def atualizar_playlists(mapa):
    afetadas = [p for p in ler_playlists() if any(x in mapa for x in p["faixas"])]
    if not afetadas:
        return
    guardar_playlists(afetadas)
    for p in afetadas:
        velhas = [x for x in dict.fromkeys(p["faixas"]) if x in mapa]
        novas = [n for n in dict.fromkeys(mapa[x] for x in velhas) if n]
        trocar_na_playlist(p, velhas, novas)
    T.msg(tr(f"   {len(afetadas)} playlists atualizadas (cópia de antes em dados/playlists_antes)",
             f"   {len(afetadas)} playlists updated (previous order saved in dados/playlists_antes)"))


def apagar(ids):
    ids, falhas = list(ids), set()
    for lote in em_lotes(ids, 300):
        r = osa(AS_APAGAR, [",".join(lote)])
        falhas |= {x.strip() for x in r.split(",") if x.strip()}
    return set(ids) - falhas


def registrar(pares, por_id, apagadas, motivo):
    agora = datetime.now().isoformat(timespec="seconds")
    with REGISTRO.open("a", encoding="utf-8") as f:
        for o, n in pares:
            a, b = por_id.get(o, {}), (por_id.get(n, {}) if n else None)
            f.write(json.dumps({
                "quando": agora, "motivo": motivo, "apagada": o in apagadas,
                "antiga": {k: a.get(k) for k in ("persistentID", "name", "artist", "album", "cloudStatus",
                                                  "kind", "bitRate", "playedCount", "arquivo")},
                "nova": {k: b.get(k) for k in ("persistentID", "name", "artist", "album", "cloudStatus")}
                if b else None}, ensure_ascii=False) + "\n")


def trocar(pares, faixas, motivo):
    """pares = [(id_antiga, id_nova)]. Faz backup, copia dados, ajusta playlists e apaga a antiga."""
    por_id = {t["id"]: t for t in faixas}
    pares = [(o, n) for o, n in pares if o in por_id and n in por_id and o != n]
    if not pares:
        return set()
    precisam = sorted({o for o, _ in pares if por_id[o]["cloudStatus"] in STATUS_BACKUP or por_id[o].get("arquivo")})
    salvos = fazer_backup(precisam, por_id)
    pares = [(o, n) for o, n in pares if o not in precisam or o in salvos]
    if not pares:
        return set()
    juntar_metadados(pares, por_id)
    atualizar_playlists(dict(pares))
    apagadas = apagar(o for o, _ in pares)
    registrar(pares, por_id, apagadas, motivo)
    falhas = len(pares) - len(apagadas)
    T.msg(tr(f"   {len(apagadas)} faixas antigas removidas da biblioteca",
             f"   {len(apagadas)} old tracks removed from the library")
          + (tr(f"; {falhas} não consegui remover", f"; {falhas} could not be removed") if falhas else ""))
    return apagadas


def esperar_novas(esperado, excluir, limite=600, parado=90, repostar=False):
    """esperado: chave → (nome, álbum, duração), nomes já normalizados.
    Espera as faixas do catálogo chegarem ao Mac; desiste depois de `parado` s sem nenhuma nova.
    repostar=True (chaves = ids do catálogo): repete o pedido de adicionar a cada 30 s, porque logo
    depois de apagar a versão antiga a Apple ainda ignora o pedido."""
    achados, inicio = {}, time.time()
    ultimo = ultimo_post = inicio
    while True:
        if repostar and time.time() - ultimo_post >= 30:
            faltam = [k for k in esperado if k not in achados]
            for ch in em_lotes(faltam, 50):
                AM.req("POST", "/v1/me/library", {"ids[songs]": ",".join(ch)}, usuario=True)
            ultimo_post = time.time()
        faixas = exportar_biblioteca()
        indice = defaultdict(list)
        for t in faixas:
            if t["cloudStatus"] == "subscription" and t["id"] not in excluir:
                indice[(norm(t["name"]), norm(t["album"]))].append(t)
        antes = len(achados)
        for k, (nome, album, dur) in esperado.items():
            if k not in achados:
                cands = [t for t in indice.get((nome, album), []) if abs((t["duration"] or 0) - dur) <= 3]
                if cands:
                    achados[k] = max(cands, key=lambda t: t["dateAdded"] or "")["id"]
        agora = time.time()
        if len(achados) > antes:
            ultimo = agora
        if len(achados) == len(esperado) or agora - inicio > limite or agora - ultimo > parado:
            return achados, faixas
        T.msg(tr(f"   aguardando o iCloud: {len(achados)}/{len(esperado)} faixas novas apareceram no Mac",
                 f"   waiting for iCloud: {len(achados)}/{len(esperado)} new tracks have reached the Mac"))
        time.sleep(15)


PENDENTES = DADOS / "pendentes.json"
PEND_LOCK = threading.RLock()
CAMPOS_PEND = ("id", "persistentID", "name", "artist", "album", "cloudStatus", "kind", "bitRate",
               "playedCount", "playedDate", "rating", "favorited", "disliked", "arquivo")
_ultima_nuvem = [0.0]


def carregar_pendentes():
    return carregar(PENDENTES, []) or []


def acionar_atualizacao_nuvem():
    """Arquivo › Biblioteca › Atualizar Biblioteca na Nuvem (no máximo a cada 3 min; se falhar, tudo bem)."""
    if time.time() - _ultima_nuvem[0] < 180:
        return
    _ultima_nuvem[0] = time.time()
    for arq, bib, item in (("Arquivo", "Biblioteca", "Atualizar Biblioteca na Nuvem"),
                           ("File", "Library", "Update Cloud Library")):
        try:
            osa(f'tell application "System Events" to tell process "Music" to click menu item "{item}" '
                f'of menu 1 of menu item "{bib}" of menu 1 of menu bar item "{arq}" of menu bar 1', timeout=30)
            return
        except Exception:  # noqa: BLE001
            pass


def fechar_devolvidas(pend, faixas, olds_todos, espera=20 * 60):
    """O iCloud às vezes devolve a faixa antiga apagada (mesmo álbum, mesmas reproduções, ID novo, fora das
    playlists). Enquanto ela existir, a versão do catálogo não entra. Fecha a pendência com ela: volta às
    playlists, recebe as reproduções que faltarem e sai da lista. Devolve as pendências que continuam."""
    agora = datetime.now(timezone.utc)
    candidatas = defaultdict(list)
    for t in faixas:
        if t["cloudStatus"] != "subscription" and t["id"] not in olds_todos and t["dateAdded"]:
            candidatas[norm(t["name"])].append(t)
    fechadas, resto, usados = [], [], set()
    for e in pend:
        criado = datetime.fromisoformat(e["criado"]).astimezone(timezone.utc)
        olds = list(e["olds"].values())
        albuns = {norm(o.get("album") or "") for o in olds}
        artistas = [o.get("artist") or "" for o in olds]
        c = [t for t in candidatas.get(e["esperado"][0], [])
             if t["id"] not in usados and agora - criado > timedelta(seconds=espera)
             and datetime.fromisoformat(t["dateAdded"].replace("Z", "+00:00")) >= criado - timedelta(minutes=2)
             and norm(t["album"]) in albuns and abs((t["duration"] or 0) - e["esperado"][2]) <= 3
             and any(sim_artista(t["artist"] or "", a) >= 0.85 for a in artistas)]
        if c:
            t = c[0]
            usados.add(t["id"])
            fechadas.append((e, t))
        else:
            resto.append(e)
    if not fechadas:
        return pend
    ops = []
    for e, t in fechadas:
        olds = list(e["olds"].values())
        op = {"id": t["id"]}
        soma = sum(o.get("playedCount") or 0 for o in olds)
        if soma > (t.get("playedCount") or 0):
            op["playedCount"] = soma
        datas = [d for d in [t.get("playedDate")] + [o.get("playedDate") for o in olds] if d]
        if datas and max(datas) != t.get("playedDate"):
            op["playedDate"] = max(datas)
        nota = max([o.get("rating") or 0 for o in olds])
        if nota and not t.get("rating"):
            op["rating"] = nota
        if any(o.get("favorited") for o in olds) and not t.get("favorited"):
            op["favorited"] = True
        if len(op) > 1:
            ops.append(op)
    for lote in em_lotes(ops, 150):
        osa(JS_METADADOS, [json.dumps(lote)], js=True)
    insercoes = defaultdict(list)
    for e, t in fechadas:
        for pl in e["playlists"]:
            insercoes[pl["pid"]].append(t["id"])
    if insercoes:
        atuais = {x["pid"]: x for x in ler_playlists()}
        for pid, ids in insercoes.items():
            if pid in atuais:
                trocar_na_playlist(atuais[pid], [], [i for i in dict.fromkeys(ids) if i not in atuais[pid]["faixas"]])
    por_id = {t["id"]: t for t in faixas}
    for e, _ in fechadas:
        por_id.update(e["olds"])
    pares = [(o, t["id"]) for e, t in fechadas for o in e["olds"]]
    registrar(pares, por_id, {o for o, _ in pares}, "iCloud devolveu a antiga")
    T.msg(tr(f"   {len(fechadas)} faixas antigas foram devolvidas pelo iCloud e bloqueiam a versão do catálogo: "
             "ficam como estavam, de volta às playlists",
             f"   {len(fechadas)} old tracks were brought back by iCloud and block the catalog version: "
             "they stay as they were, back in their playlists"))
    return resto


def concluir_pendentes():
    """Para cada troca já feita cuja faixa nova chegou ao Mac: passa reproduções/nota/favorito e põe a
    faixa nova na posição da antiga em cada playlist. Devolve quantas ainda faltam."""
    with PEND_LOCK:
        pend = carregar_pendentes()
        if not pend:
            return 0
        faixas = exportar_biblioteca()
        olds_todos = {o for e in pend for o in e["olds"]}
        indice = defaultdict(list)
        for t in faixas:
            if t["cloudStatus"] == "subscription" and t["id"] not in olds_todos:
                indice[(norm(t["name"]), norm(t["album"]))].append(t)
        por_nome = defaultdict(list)
        for lst in indice.values():
            for t in lst:
                por_nome[norm(t["name"])].append(t)
        prontos, resto, usados = [], [], set()
        for e in pend:
            nome, album, dur = e["esperado"]
            c = [t for t in indice.get((nome, album), []) if abs((t["duration"] or 0) - dur) <= 3]
            if not c:
                # Às vezes a música chega com o nome de outro álbum (o single ou a "Canción de la Semana"
                # em vez do álbum do catálogo). Aceita pelo nome, artista e duração, se entrou depois do pedido.
                desde = datetime.fromisoformat(e["criado"]).astimezone(timezone.utc) - timedelta(minutes=2)
                artistas = [o.get("artist") or "" for o in e["olds"].values()]
                c = [t for t in por_nome.get(nome, [])
                     if t["id"] not in usados and abs((t["duration"] or 0) - dur) <= 2
                     and t["dateAdded"] and datetime.fromisoformat(t["dateAdded"].replace("Z", "+00:00")) >= desde
                     and any(sim_artista(t["artist"] or "", a) >= 0.85 for a in artistas)]
            if c:
                n = max(c, key=lambda t: t["dateAdded"] or "")["id"]
                usados.add(n)
                prontos.append((e, n))
            else:
                resto.append(e)
        resto = fechar_devolvidas(resto, faixas, olds_todos)
        agora = time.time()
        repedir = [e for e in resto if agora - e.get("ultimoPedido", 0) > 300]
        if repedir and AM.user:
            try:
                # Pede de novo só as que ainda não estão na biblioteca da nuvem, uma por vez: pedidos em
                # lote às vezes são aceitos (202) e só parte entra.
                fora = []
                for ch in em_lotes([e["sid"] for e in repedir], 50):
                    r = AM.req("GET", f"/v1/catalog/{AM.sf}/songs", {"ids": ",".join(ch), "relate": "library"},
                               usuario=True)
                    na_nuvem = {s["id"] for s in r.get("data", [])
                                if s.get("relationships", {}).get("library", {}).get("data")}
                    fora += [i for i in ch if i not in na_nuvem]
                for sid in fora:
                    AM.req("POST", "/v1/me/library", {"ids[songs]": sid}, usuario=True)
                for e in repedir:
                    e["ultimoPedido"] = agora
            except Exception as err:  # noqa: BLE001
                T.msg(tr(f"   não consegui pedir de novo ao Apple Music: {err}",
                         f"   could not ask Apple Music again: {err}"))
        if prontos:
            por_id = {t["id"]: t for t in faixas}
            for e, _ in prontos:
                por_id.update(e["olds"])
            pares = [(o, n) for e, n in prontos for o in e["olds"]]
            juntar_metadados(pares, por_id)
            insercoes = defaultdict(list)
            for e, n in prontos:
                for pl in e["playlists"]:
                    insercoes[pl["pid"]].append((pl, n))
            if insercoes:
                atuais = {x["pid"]: x for x in ler_playlists()}
                for pid, lst in insercoes.items():
                    if pid in atuais:
                        trocar_na_playlist(atuais[pid], [], list(dict.fromkeys(n for _, n in lst)))
            registrar(pares, por_id, {o for o, _ in pares}, "catálogo")
            T.msg(tr(f"   {len(prontos)} músicas novas chegaram do iCloud: reproduções e playlists acertadas"
                     f" (faltam {len(resto)})",
                     f"   {len(prontos)} new songs arrived from iCloud: play counts and playlists updated"
                     f" ({len(resto)} to go)"))
        salvar(PENDENTES, resto)
        return len(resto)


def aguardar_pendentes(limite, alvo, sem_progresso):
    """Se houver mais que `limite` pendências, espera o iCloud até baixar para `alvo`. O iCloud às vezes
    leva mais de uma hora para entregar; só desiste depois de `sem_progresso` segundos sem nenhuma chegar."""
    n = concluir_pendentes()
    if n <= limite:
        return n
    ultimo_avanco = ultimo_aviso = time.time()
    T.msg(tr(f"   {n} músicas aguardando o iCloud; espero baixar antes de continuar…",
             f"   {n} songs waiting for iCloud; waiting for it to catch up before continuing…"))
    while n > alvo and time.time() - ultimo_avanco < sem_progresso:
        time.sleep(30)
        acionar_atualizacao_nuvem()
        antes, n = n, concluir_pendentes()
        if n < antes:
            ultimo_avanco = time.time()
        if n != antes or time.time() - ultimo_aviso >= 300:
            ultimo_aviso = time.time()
            minutos = int((time.time() - ultimo_avanco) // 60)
            T.msg(tr(f"   aguardando o iCloud: {n} músicas ainda não chegaram ao Mac"
                     + (f" (nenhuma chegou nos últimos {minutos} min)" if minutos >= 5 else ""),
                     f"   waiting for iCloud: {n} songs have not reached the Mac yet"
                     + (f" (none arrived in the last {minutos} min)" if minutos >= 5 else "")))
    return n


def trocar_bloqueadas(bloq, faixas, motivo):
    """A antiga é Enviada/Correspondida (ou há outra assim com o mesmo nome e álbum): enquanto ela existir,
    adicionar a versão do catálogo não cria faixa nova. Ordem: backup → anota tudo em pendentes.json →
    apaga → pede a nova. Quando a nova chega ao Mac, concluir_pendentes passa os dados e as playlists."""
    por_id = {t["id"]: t for t in faixas}
    bloq = [b for b in bloq if all(o in por_id for o in b["olds"])]
    if not bloq:
        return set()
    todos = sorted({o for b in bloq for o in b["olds"]})
    precisam = [o for o in todos if por_id[o]["cloudStatus"] in STATUS_BACKUP or por_id[o].get("arquivo")]
    salvos = fazer_backup(precisam, por_id)
    bloq = [b for b in bloq if all(o not in precisam or o in salvos for o in b["olds"])]
    if not bloq:
        return set()
    olds = {o for b in bloq for o in b["olds"]}
    afetadas = [p for p in ler_playlists() if any(x in olds for x in p["faixas"])]
    if afetadas:
        guardar_playlists(afetadas)
    agora = datetime.now().isoformat(timespec="seconds")
    entradas = []
    for b in bloq:
        e = {"sid": b["sid"], "esperado": list(b["esperado"]), "motivo": motivo, "criado": agora,
             "ultimoPedido": time.time(), "playlists": [],
             "olds": {o: {k: por_id[o].get(k) for k in CAMPOS_PEND} for o in b["olds"]}}
        for p in afetadas:
            fx = p["faixas"]
            for idx, x in enumerate(fx):
                if x in b["olds"]:
                    antes = [y for y in reversed(fx[max(0, idx - 15):idx]) if y not in olds]
                    e["playlists"].append({"pid": p["pid"], "nome": p["nome"], "pos": idx, "antes": antes})
        entradas.append(e)
    with PEND_LOCK:  # anota antes de apagar: se algo for interrompido, nada se perde
        salvar(PENDENTES, carregar_pendentes() + entradas)
    apagadas = apagar(olds)
    if apagadas != olds:  # as que não saíram continuam na biblioteca; tira a pendência delas
        ruins = {(e["sid"], e["criado"]) for e, b in zip(entradas, bloq) if not b["olds"] <= apagadas}
        with PEND_LOCK:
            salvar(PENDENTES, [e for e in carregar_pendentes() if (e["sid"], e["criado"]) not in ruins])
        bloq = [b for b in bloq if b["olds"] <= apagadas]
    for ch in em_lotes([b["sid"] for b in bloq], 50):
        AM.req("POST", "/v1/me/library", {"ids[songs]": ",".join(ch)}, usuario=True)
    acionar_atualizacao_nuvem()
    T.msg(tr(f"   {len(apagadas)} faixas antigas saíram; as versões do catálogo foram pedidas e entram assim que "
             "o iCloud trouxer",
             f"   {len(apagadas)} old tracks removed; the catalog versions were requested and come in as soon as "
             "iCloud delivers them"))
    return apagadas


# ───────────────────────── aplicar trocas pelo catálogo ─────────────────────────

VERSAO_ANALISE = 2  # 2 = faixas identificadas pelo persistent ID


def job_aplicar(ids):
    an = carregar(ANALISE, {})
    if an.get("versao") != VERSAO_ANALISE:
        raise RuntimeError(tr("Esta análise é de uma versão anterior. Clique em Analisar biblioteca de novo.",
                              "This analysis is from an older version. Click Analyze library again."))
    escolhidos = [i for i in an.get("itens", []) if i["id"] in set(ids) and not i.get("feito")]
    if not escolhidos:
        raise RuntimeError(tr("Nada selecionado (ou já aplicado).", "Nothing selected (or already applied)."))
    T.passo(tr(f"Trocando {len(escolhidos)} álbuns…", f"Swapping {len(escolhidos)} albums…"), len(escolhidos))
    for lote in em_lotes(escolhidos, 25):
        if aguardar_pendentes(limite=150, alvo=60, sem_progresso=3 * 3600) > 150:
            raise RuntimeError(tr("O iCloud não trouxe nenhuma música nova em 3 horas. Parei para não acumular; "
                                  "o que já foi trocado se completa sozinho quando elas chegarem. Clique em "
                                  "Trocar de novo mais tarde para continuar de onde parou.",
                                  "iCloud delivered no new songs in 3 hours. Stopped so they don't pile up; "
                                  "what was already swapped completes itself when they arrive. Click Swap "
                                  "again later to continue where it stopped."))
        aplicar_lote(lote)
        T.feito += len(lote)
        estado = {i["id"]: i for i in lote}
        an = carregar(ANALISE, {})
        for i in an.get("itens", []):
            if i["id"] in estado:
                i.update({k: estado[i["id"]].get(k) for k in ("feito", "pendentes")})
        salvar(ANALISE, an)
    T.passo(tr("Aguardando o iCloud trazer as últimas músicas…", "Waiting for iCloud to deliver the last songs…"))
    n = aguardar_pendentes(limite=0, alvo=0, sem_progresso=30 * 60)
    if n:
        T.msg(tr(f"{n} músicas ainda não chegaram ao Mac. Elas se completam sozinhas quando chegarem, "
                 "com esta página aberta (ou da próxima vez que você abrir).",
                 f"{n} songs have not reached the Mac yet. They complete themselves when they arrive, "
                 "while this page is open (or next time you open it)."))


def aplicar_lote(lote):
    faixas = exportar_biblioteca()
    por_id = {t["id"]: t for t in faixas}
    # Só mexe numa faixa antiga se ela ainda for exatamente a que a análise viu.
    validas, mudaram = {}, 0
    for i in lote:
        for f in i["faixas"]:
            t = por_id.get(f["old"])
            if t and norm(t["name"]) == norm(f["nome"]) and abs((t["duration"] or 0) - (f["dur"] or 0)) <= 2:
                validas[(i["id"], f["old"])] = (f, i["catalogo"]["nome"])
            else:
                mudaram += 1
    if mudaram:
        T.msg(tr(f"   {mudaram} faixas mudaram desde a análise; ficam como estão.",
                 f"   {mudaram} tracks changed since the analysis; left as they are."))
    apagadas = set()
    # Faixa antiga Enviada/Correspondida fica ligada à música do catálogo: adicionar a versão do
    # catálogo não cria faixa nova enquanto ela existir. O mesmo vale se outra faixa Correspondida/Enviada
    # com o mesmo nome e álbum estiver na biblioteca. Esses casos vão direto para trocar_bloqueadas.
    ocup_idx = defaultdict(list)
    for t in faixas:
        if t["cloudStatus"] in ("matched", "uploaded"):
            ocup_idx[(norm(t["name"]), norm(t["album"]))].append(t)
    bloq = {}
    for k, (f, alb) in list(validas.items()):
        nome, album, dur = norm(f["novoNome"]), norm(alb), f["novoDur"]
        ocupantes = [t for t in ocup_idx.get((nome, album), []) if abs((t["duration"] or 0) - dur) <= 3]
        if ocupantes or por_id[f["old"]]["cloudStatus"] in ("matched", "uploaded", "unknown"):
            b = bloq.setdefault(f["new"], {"sid": f["new"], "esperado": (nome, album, dur), "olds": set()})
            b["olds"].add(f["old"])
            b["olds"].update(t["id"] for t in ocupantes)
            del validas[k]
    if bloq:
        apagadas |= trocar_bloqueadas(list(bloq.values()), faixas, "catálogo")
        faixas = exportar_biblioteca()
    if validas:
        songs = list(dict.fromkeys(f["new"] for f, _ in validas.values()))
        T.msg(tr(f"Adicionando {len(songs)} faixas do catálogo à biblioteca ",
                 f"Adding {len(songs)} catalog tracks to the library ")
              + f"({', '.join(i['album'] for i in lote[:3])}{'…' if len(lote) > 3 else ''})")
        for ch in em_lotes(songs, 50):
            AM.req("POST", "/v1/me/library", {"ids[songs]": ",".join(ch)}, usuario=True)
        esperado = {k: (norm(f["novoNome"]), norm(alb), f["novoDur"]) for k, (f, alb) in validas.items()}
        antigos = {f["old"] for f, _ in validas.values()}
        achados, faixas = esperar_novas(esperado, antigos, parado=60)
        apagadas |= trocar([(k[1], n) for k, n in achados.items()], faixas, "catálogo")
        # As que não chegaram: a música do catálogo já existe na biblioteca como Correspondida/Enviada?
        bloq = {}
        for k, (f, _) in validas.items():
            if k in achados or f["old"] in apagadas:
                continue
            nome, album, dur = esperado[k]
            ocupantes = [t for t in faixas if t["cloudStatus"] in ("matched", "uploaded")
                         and norm(t["name"]) == nome and norm(t["album"]) == album
                         and abs((t["duration"] or 0) - dur) <= 3]
            if ocupantes:
                b = bloq.setdefault(f["new"], {"sid": f["new"], "esperado": esperado[k], "olds": set()})
                b["olds"].add(f["old"])
                b["olds"].update(t["id"] for t in ocupantes)
        if bloq:
            apagadas |= trocar_bloqueadas(list(bloq.values()), faixas, "catálogo")
    for i in lote:
        pend = [f["nome"] for f in i["faixas"] if f["old"] not in apagadas]
        i["feito"] = not pend
        i["pendentes"] = pend


# ───────────────────────── repetidas ─────────────────────────

def qualidade(t):
    k = sem_acento(t.get("kind") or "").lower()
    sem_perda = any(x in k for x in ("lossless", "wav", "aiff", "flac")) and bool(t.get("arquivo"))
    return (sem_perda, RANK_STATUS.get(t["cloudStatus"], 1), t.get("bitRate") or 0,
            t.get("sampleRate") or 0, t.get("playedCount") or 0)


def achar_repetidas(faixas):
    g = defaultdict(list)
    for t in faixas:
        if e_musica(t) and t["name"]:
            g[(norm(t["name"]), norm_artista(t["artist"]), norm(t["album"]))].append(t)
    grupos = []
    for chave, ts in g.items():
        if len(ts) < 2:
            continue
        ts.sort(key=lambda t: t["duration"] or 0)
        blocos = [[ts[0]]]
        for t in ts[1:]:
            if abs((t["duration"] or 0) - (blocos[-1][0]["duration"] or 0)) <= 3:
                blocos[-1].append(t)
            else:
                blocos.append([t])
        for b in blocos:
            if len(b) < 2:
                continue
            b = sorted(b, key=qualidade, reverse=True)
            gid = hashlib.sha1(f"{chave}|{round(b[0]['duration'] or 0)}".encode()).hexdigest()[:12]
            resumo = lambda t: {k: t.get(k) for k in ("id", "cloudStatus", "kind", "bitRate",  # noqa: E731
                                                     "playedCount", "dateAdded")}
            grupos.append({"id": gid, "nome": b[0]["name"], "artista": b[0]["artist"], "album": b[0]["album"],
                           "manter": resumo(b[0]), "remover": [resumo(t) for t in b[1:]]})
    grupos.sort(key=lambda x: (norm(x["artista"]), norm(x["album"]), norm(x["nome"])))
    return grupos


def job_repetidas(ids):
    T.passo(tr("Lendo a biblioteca…", "Reading the library…"))
    faixas = exportar_biblioteca()
    grupos = [g for g in achar_repetidas(faixas) if g["id"] in set(ids)]
    pares = [(r["id"], g["manter"]["id"]) for g in grupos for r in g["remover"]]
    T.passo(tr(f"Removendo {len(pares)} repetidas de {len(grupos)} músicas…",
               f"Removing {len(pares)} duplicates of {len(grupos)} songs…"))
    trocar(pares, faixas, "repetida")


# ───────────────────────── álbuns sem equivalente no catálogo ─────────────────────────

def chave_sem_par(artista, album):
    return f"{norm_artista(artista or '')}|{norm(album or '')}"


def grupos_sem_par(faixas):
    """Os álbuns que a última análise não achou no catálogo, lidos de novo da biblioteca (com as faixas
    de agora). Cada grupo traz o id usado para apagar."""
    an = carregar(ANALISE, {}) or {}
    if an.get("versao") != VERSAO_ANALISE:
        raise RuntimeError(tr("Analise a biblioteca primeiro.", "Analyze the library first."))
    chaves = {chave_sem_par(i["artista"], i["album"]) for i in an.get("naoAchados", [])}
    saida = []
    for g in agrupar(faixas):
        if g["tipo"] != "trocar":
            continue
        if chave_sem_par(g["artista"], g["album"] or g["faixas"][0]["name"]) not in chaves:
            continue
        saida.append({"id": g["id"], "artista": g["artista"], "album": g["album"] or g["faixas"][0]["name"],
                      "faixas": [{"id": t["id"], "nome": t["name"], "artista": t["artist"],
                                  "status": t["cloudStatus"], "dur": t["duration"],
                                  "reproducoes": t.get("playedCount") or 0, "arquivo": bool(t.get("arquivo"))}
                                 for t in g["faixas"]]})
    saida.sort(key=lambda g: (norm(g["artista"]), norm(g["album"])))
    return saida


def job_apagar_sem_par(ids):
    """Apaga da biblioteca (e do iCloud) os álbuns escolhidos que não têm equivalente no catálogo.
    Antes, baixa e copia para o backup as faixas que são arquivos seus; as que não baixarem ficam."""
    T.passo(tr("Lendo a biblioteca…", "Reading the library…"))
    faixas = exportar_biblioteca()
    por_id = {t["id"]: t for t in faixas}
    grupos = [g for g in grupos_sem_par(faixas) if g["id"] in set(ids)]
    alvo = [f["id"] for g in grupos for f in g["faixas"]]
    if not alvo:
        raise RuntimeError(tr("Nada para apagar (os álbuns marcados não estão mais na biblioteca).",
                              "Nothing to delete (the selected albums are no longer in the library)."))
    T.passo(tr(f"Apagando {len(grupos)} álbuns sem equivalente ({len(alvo)} faixas)…",
               f"Deleting {len(grupos)} albums with no catalog match ({len(alvo)} tracks)…"))
    precisam = sorted(i for i in alvo if por_id[i]["cloudStatus"] in STATUS_BACKUP or por_id[i].get("arquivo"))
    salvos = fazer_backup(precisam, por_id)
    podem = [i for i in alvo if i not in precisam or i in salvos]
    if len(podem) < len(alvo):
        T.msg(tr(f"   {len(alvo) - len(podem)} faixas ficam na biblioteca porque o backup não deu certo.",
                 f"   {len(alvo) - len(podem)} tracks stay in the library because the backup did not work."))
    if not podem:
        return
    afetadas = [p for p in ler_playlists() if any(x in set(podem) for x in p["faixas"])]
    if afetadas:
        guardar_playlists(afetadas)
    apagadas = apagar(podem)
    registrar([(i, None) for i in podem], por_id, apagadas, "sem equivalente")
    falhas = len(podem) - len(apagadas)
    T.msg(tr(f"   {len(apagadas)} faixas apagadas da biblioteca"
             + (f"; saíram também de {len(afetadas)} playlists (cópia de antes em dados/playlists_antes)"
                if afetadas else ""),
             f"   {len(apagadas)} tracks deleted from the library"
             + (f"; also removed from {len(afetadas)} playlists (previous order saved in dados/playlists_antes)"
                if afetadas else ""))
          + (tr(f"; {falhas} não consegui apagar", f"; {falhas} could not be deleted") if falhas else ""))


# ───────────────────────── servidor local ─────────────────────────

NOMES_BACKUP = ("Backup versões antigas", "Old versions backup")


def pastas_backup():
    return [Path.home() / "Music" / n for n in NOMES_BACKUP if (Path.home() / "Music" / n).is_dir()]


def info_backup():
    arquivos = tamanho = 0
    for pasta in pastas_backup():
        for f in pasta.rglob("*"):
            if f.is_file() and not f.name.startswith("."):
                arquivos += 1
                tamanho += f.stat().st_size
    return {"pastas": [str(x) for x in pastas_backup()], "arquivos": arquivos, "bytes": tamanho}


def para_o_lixo(pasta):
    """Manda a pasta para o Lixo (recuperável até esvaziar). Tenta pelo Finder; senão move para ~/.Trash."""
    try:
        osa('on run argv\n\ttell application "Finder" to delete (POSIX file (item 1 of argv) as alias)\nend run',
            [str(pasta)], timeout=120)
        if not pasta.exists():
            return
    except Exception:  # noqa: BLE001
        pass
    lixo = Path.home() / ".Trash"
    alvo = lixo / pasta.name
    if alvo.exists():
        alvo = lixo / f"{pasta.name} {time.strftime('%Y-%m-%d %H.%M.%S')}"
    shutil.move(str(pasta), str(alvo))


def apagar_backup():
    if T.rodando:
        raise RuntimeError(tr("Espere a tarefa em andamento terminar.", "Wait for the running task to finish."))
    n = len(carregar_pendentes())
    if n:
        raise RuntimeError(tr(f"{n} músicas ainda aguardam o iCloud; até elas chegarem, o backup é a única cópia. "
                              "Tente de novo depois.",
                              f"{n} songs are still waiting for iCloud; until they arrive the backup is the only "
                              "copy. Try again later."))
    for pasta in pastas_backup():
        para_o_lixo(pasta)
    T.msg(tr("Backup das versões antigas enviado para o Lixo.", "Old versions backup moved to the Trash."))


def estado():
    return {"configurado": bool(AM.dev), "devToken": AM.dev, "logado": bool(AM.user), "storefront": AM.sf,
            "tarefa": T.estado(), "temAnalise": ANALISE.exists(), "pendentes": len(carregar_pendentes()), "backup": str(pasta_backup()), "idioma": IDIOMA["atual"],
            "config": {k: v for k, v in (carregar(CONFIG, {}) or {}).items()}}


def configurar(cfg):
    caminho = os.path.expanduser(cfg.get("key_path", "").strip())
    if not Path(caminho).is_file():
        raise RuntimeError(tr(f"Não achei o arquivo .p8 em {caminho}", f".p8 file not found at {caminho}"))
    dev = gerar_token(cfg["team_id"].strip(), cfg["key_id"].strip(), caminho)
    antigo, AM.dev = AM.dev, dev
    try:
        AM.req("GET", f"/v1/catalog/{AM.sf}/search", {"term": "beatles", "types": "songs", "limit": "1"})
    except Exception:
        AM.dev = antigo
        raise
    salvar(CONFIG, {"team_id": cfg["team_id"].strip(), "key_id": cfg["key_id"].strip(), "key_path": caminho})


def entrar(token):
    AM.user = token
    try:
        d = AM.req("GET", "/v1/me/storefront", usuario=True)
        AM.sf = (d.get("data") or [{}])[0].get("id") or AM.sf
    except Exception:
        AM.user = None
        raise
    salvar(TOKEN, {"token": token, "storefront": AM.sf}, privado=True)


class Pagina(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _resp(self, corpo, tipo="application/json; charset=utf-8", codigo=200):
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corpo)

    def _json(self, obj, codigo=200):
        self._resp(json.dumps(obj, ensure_ascii=False).encode(), codigo=codigo)

    def _host_ok(self):
        return self.headers.get("Host", "") in (f"localhost:{PORTA}", f"127.0.0.1:{PORTA}")

    def do_GET(self):
        if not self._host_ok():
            return self._json({"erro": "host"}, 403)
        caminho = urllib.parse.urlparse(self.path).path
        try:
            if caminho == "/":
                return self._resp((BASE / "pagina.html").read_bytes(), "text/html; charset=utf-8")
            if caminho == "/api/estado":
                return self._json(estado())
            if caminho == "/api/backup":
                return self._json(info_backup())
            if caminho == "/api/analise":
                return self._json(carregar(ANALISE, {}) or {})
            if caminho == "/api/repetidas":
                return self._json({"grupos": achar_repetidas(exportar_biblioteca())})
            if caminho == "/api/sempar":
                return self._json({"grupos": grupos_sem_par(exportar_biblioteca())})
            self._json({"erro": "não encontrado"}, 404)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._json({"erro": str(e)}, 500)

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-Troca") != "1":
            return self._json({"erro": "origem"}, 403)
        caminho = urllib.parse.urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length") or 0)
            corpo = json.loads(self.rfile.read(n) or b"{}")
            if caminho == "/api/idioma":
                IDIOMA["atual"] = "en" if corpo.get("idioma") == "en" else "pt"
                salvar(PREFS, {"idioma": IDIOMA["atual"]})
            elif caminho == "/api/config":
                configurar(corpo)
            elif caminho == "/api/login":
                entrar(corpo["token"])
            elif caminho == "/api/analisar":
                if not AM.user:
                    raise RuntimeError(tr("Entre no Apple Music primeiro.", "Sign in to Apple Music first."))
                T.iniciar(tr("Análise", "Analysis"), job_analisar)
            elif caminho == "/api/aplicar":
                T.iniciar(tr("Troca", "Swap"), job_aplicar, corpo.get("ids") or [])
            elif caminho == "/api/backup/apagar":
                apagar_backup()
            elif caminho == "/api/repetidas":
                T.iniciar(tr("Repetidas", "Duplicates"), job_repetidas, corpo.get("ids") or [])
            elif caminho == "/api/sempar":
                T.iniciar(tr("Sem equivalente", "No catalog match"), job_apagar_sem_par, corpo.get("ids") or [])
            else:
                return self._json({"erro": "não encontrado"}, 404)
            self._json(estado())
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._json({"erro": str(e)}, 400)


def iniciar():
    DADOS.mkdir(exist_ok=True)
    IDIOMA["atual"] = (carregar(PREFS, {}) or {}).get("idioma", "pt")
    cfg = carregar(CONFIG)
    if cfg:
        try:
            AM.dev = gerar_token(cfg["team_id"], cfg["key_id"], cfg["key_path"])
        except Exception as e:  # noqa: BLE001
            print("Chave MusicKit:", e)
    tok = carregar(TOKEN)
    if tok:
        AM.user, AM.sf = tok.get("token"), tok.get("storefront") or "br"
    def vigia():  # conclui trocas pendentes quando as músicas novas chegam, mesmo sem tarefa rodando
        while True:
            time.sleep(120)
            try:
                if not T.rodando and carregar_pendentes():
                    acionar_atualizacao_nuvem()
                    concluir_pendentes()
            except Exception as e:  # noqa: BLE001
                print("pendentes:", e)
    threading.Thread(target=vigia, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORTA), Pagina)
    url = f"http://localhost:{PORTA}"
    print(tr(f"Music Lossless em {url}  (Ctrl+C para sair)", f"Music Lossless at {url}  (Ctrl+C to quit)"))
    if "--sem-navegador" not in sys.argv:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        AM.salvar_cache(True)


if __name__ == "__main__":
    iniciar()
