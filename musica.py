#!/usr/bin/env python3
"""
Troca Lossless — app Música no Mac.

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
from datetime import datetime
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
BACKUP = Path.home() / "Music" / "Backup versões antigas"
PLAYLIST_BACKUP = "zz Backup antes da troca (automático)"
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
                raise RuntimeError("Já existe uma tarefa em andamento.")
            self.rodando, self.nome, self.etapa = True, nome, ""
            self.feito = self.total = 0
            self.erro = None

        def alvo():
            try:
                fn(*args)
                self.msg("Concluído.")
            except Exception as e:  # noqa: BLE001
                self.erro = str(e)
                self.msg("ERRO: " + str(e))
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
        raise RuntimeError("app Música: " + r.stderr.strip()[-400:])
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
    const f = lib.fileTracks, ids = f.databaseID(), loc = f.location();
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
    const ids = f.databaseID(), loc = f.location();
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
    try { ids = ps[i].tracks.databaseID(); } catch (e) {}
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
      const t = lib.tracks.whose({ databaseID: o.id })[0];
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

AS_REFAZER_PLAYLIST = r"""
on run argv
	set pid to item 1 of argv
	set inicio to (item 2 of argv) as integer
	set esperado to (item 3 of argv) as integer
	set AppleScript's text item delimiters to ","
	set ids to text items of (item 4 of argv)
	tell application "Music"
		set lib to library playlist 1
		set p to first user playlist whose persistent ID is pid
		set n to count of tracks of p
		if n is not esperado then return "mudou:" & n
		if n ≥ inicio then delete (tracks inicio thru n of p)
		set falhas to 0
		repeat with i in ids
			if (contents of i) is not "" then
				try
					duplicate (first track of lib whose database ID is ((contents of i) as integer)) to p
				on error
					set falhas to falhas + 1
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
				duplicate (first track of lib whose database ID is ((contents of i) as integer)) to p
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
				delete (first track of lib whose database ID is ((contents of i) as integer))
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
        t["arquivo"] = o["arquivos"].get(str(t["databaseID"]))
        faixas.append(t)
    return faixas


def arquivos_locais():
    return {int(k): v for k, v in json.loads(osa(JS_ARQUIVOS, js=True)).items()}


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
        raise RuntimeError("Não consegui assinar com a chave .p8: " + r.stderr.decode()[-300:])
    der = r.stdout  # SEQUENCE { INTEGER r, INTEGER s }
    i = 2 if der[1] < 0x80 else 2 + (der[1] & 0x7F)

    def inteiro(i):
        if der[i] != 2:
            raise RuntimeError("Assinatura inesperada do openssl.")
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
            raise RuntimeError("Configure a chave MusicKit primeiro.")
        if usuario and not self.user:
            raise RuntimeError("Entre no Apple Music primeiro.")
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
                    raise RuntimeError(f"Apple Music recusou o acesso ({e.code}) várias vezes seguidas. "
                                       "Confira a chave MusicKit ou entre de novo.") from e
                raise RuntimeError(f"Apple Music API {e.code}: {e.read()[:300]!r}") from e
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(min(60, 2 ** tentativa))
        raise RuntimeError("Apple Music não respondeu: " + caminho)


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
        T.msg(f"   álbuns da biblioteca na nuvem: {offset + len(d.get('data', []))}")
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
        faixas.append({"old": l["databaseID"], "nome": l["name"], "status": l["cloudStatus"],
                       "dur": l["duration"], "new": n["id"], "novoNome": n["nome"], "novoDur": n["dur"]})
    sem_par = [g["faixas"][i]["name"] for i in range(len(g["faixas"])) if i not in a["pares"]]
    info = {k: c[k] for k in ("id", "nome", "artista", "url", "variants", "arte", "ano")}
    return {"id": g["id"], "tipo": g["tipo"], "artista": g["artista"], "album": g["album"] or "(sem álbum)",
            "status": dict(Counter(t["cloudStatus"] for t in g["faixas"])), "nFaixas": len(g["faixas"]),
            "catalogo": info, "conf": ["", "baixa", "média", "alta"][a["conf"]],
            "cobertura": round(a["cob"], 3), "faixas": faixas, "semPar": sem_par}


def job_analisar():
    T.passo("Lendo a biblioteca do app Música…")
    faixas = exportar_biblioteca()
    grupos = agrupar(faixas)
    n_trocar = sum(g["tipo"] == "trocar" for g in grupos)
    T.msg(f"{len(faixas)} faixas. {n_trocar} álbuns com faixas enviadas/correspondidas/indisponíveis; "
          f"{len(grupos) - n_trocar} álbuns do Apple Music para checar se existe edição com Atmos.")
    atual = {}
    if AM.user:
        T.passo("Lendo os álbuns da biblioteca na nuvem…")
        try:
            atual = albuns_da_biblioteca()
        except Exception as e:  # noqa: BLE001
            T.msg(f"   não consegui ler (sigo sem): {e}")
    T.passo("Procurando cada álbum no catálogo…", len(grupos))
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
                T.msg(f"   falhou: {g['artista']} — {g['album']}: {erro}")
                if falhas > 25 and falhas > T.feito / 2:
                    raise RuntimeError("Muitas falhas seguidas na API; parei.")
            elif item:
                itens.append(item)
            elif g["tipo"] == "trocar":
                nao_achados.append({"artista": g["artista"], "album": g["album"] or g["faixas"][0]["name"],
                                    "status": dict(Counter(t["cloudStatus"] for t in g["faixas"])),
                                    "nFaixas": len(g["faixas"])})
            if T.feito % 50 == 0:
                T.msg(f"   {T.feito}/{len(grupos)} álbuns — {len(itens)} com versão melhor")
    AM.salvar_cache(True)
    feitos = {i["id"] for i in (carregar(ANALISE, {}) or {}).get("itens", []) if i.get("feito")}
    for i in itens:
        i["feito"] = i["id"] in feitos
    itens.sort(key=lambda i: (i["tipo"], norm(i["artista"]), norm(i["album"])))
    nao_achados.sort(key=lambda i: (norm(i["artista"]), norm(i["album"])))
    salvar(ANALISE, {"quando": datetime.now().isoformat(timespec="seconds"), "storefront": AM.sf,
                     "itens": itens, "naoAchados": nao_achados, "falhas": falhas})
    T.msg(f"Pronto: {len(itens)} álbuns com versão melhor; {len(nao_achados)} sem equivalente no catálogo."
          + (f" {falhas} falharam — analise de novo para tentar só esses (o resto vem do cache)." if falhas else ""))


# ───────────────────────── troca (comum a tudo) ─────────────────────────

def destino_backup(t, origem):
    seguro = lambda s: re.sub(r'[/:\\]', "-", (s or "Sem nome").strip())[:120] or "Sem nome"  # noqa: E731
    pasta = BACKUP / seguro(t["albumArtist"] or t["artist"]) / seguro(t["album"])
    pasta.mkdir(parents=True, exist_ok=True)
    alvo = pasta / Path(origem).name
    n = 2
    while alvo.exists() and alvo.stat().st_size != Path(origem).stat().st_size:
        alvo = pasta / f"{Path(origem).stem} ({n}){Path(origem).suffix}"
        n += 1
    return alvo


def fazer_backup(ids, por_id):
    """Baixa e copia as faixas para BACKUP. Devolve os ids que estão salvos com segurança."""
    if not ids:
        return set()
    locais = arquivos_locais()
    faltam = [i for i in ids if i not in locais]
    if faltam:
        T.msg(f"Baixando {len(faltam)} faixas antigas para o backup…")
        osa(AS_PLAYLIST_BACKUP, [PLAYLIST_BACKUP, ",".join(map(str, faltam))])
        ultimo_avanco, antes = time.time(), -1
        limite_sem_avanco = 240
        while True:
            locais = arquivos_locais()
            prontos = sum(i in locais for i in faltam)
            if prontos == len(faltam):
                break
            if prontos != antes:
                antes, ultimo_avanco = prontos, time.time()
                T.msg(f"   baixadas {prontos}/{len(faltam)}")
            elif time.time() - ultimo_avanco > limite_sem_avanco:
                T.msg(f"   {len(faltam) - prontos} não baixaram em {limite_sem_avanco // 60} min; "
                      "essas ficam na biblioteca.")
                break
            time.sleep(10)
        osa(AS_APAGAR_PLAYLIST, [PLAYLIST_BACKUP])
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
    T.msg(f"   backup: {len(ok)}/{len(ids)} faixas copiadas para {BACKUP}")
    return ok


def juntar_metadados(pares, por_id):
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
    T.msg(f"   reproduções/notas/favoritos copiados para {len(ops) - erros} faixas"
          + (f" ({erros} falharam)" if erros else ""))


def atualizar_playlists(mapa):
    pls = json.loads(osa(JS_PLAYLISTS, js=True))
    afetadas = [p for p in pls if any(x in mapa for x in p["faixas"])]
    if not afetadas:
        return
    salvar(SNAPSHOTS / f"playlists-{time.strftime('%Y%m%d-%H%M%S')}.json", afetadas)
    for p in afetadas:
        fx, conj = p["faixas"], set(p["faixas"])
        novo, postos = [], set()
        for x in fx:
            if x in mapa:
                n = mapa[x]
                if n in conj or n in postos:
                    continue  # a nova já está na playlist: só sai a antiga
                novo.append(n)
                postos.add(n)
            else:
                novo.append(x)
        inicio = 0
        while inicio < len(novo) and inicio < len(fx) and fx[inicio] == novo[inicio]:
            inicio += 1
        r = osa(AS_REFAZER_PLAYLIST, [p["pid"], inicio + 1, len(fx), ",".join(map(str, novo[inicio:]))])
        if r.startswith("mudou"):
            T.msg(f"   playlist “{p['nome']}” mudou durante a troca; não mexi nela.")
        elif r != "ok:0":
            T.msg(f"   playlist “{p['nome']}”: {r.split(':')[1]} faixas não entraram.")
    T.msg(f"   {len(afetadas)} playlists atualizadas (cópia de antes em dados/playlists_antes)")


def trocar(pares, faixas, motivo):
    """pares = [(id_antiga, id_nova)]. Faz backup, copia dados, ajusta playlists e apaga a antiga."""
    por_id = {t["databaseID"]: t for t in faixas}
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
    falhas = set()
    for lote in em_lotes([o for o, _ in pares], 300):
        r = osa(AS_APAGAR, [",".join(map(str, lote))])
        falhas |= {int(x) for x in r.split(",") if x.strip().isdigit()}
    apagadas = {o for o, _ in pares} - falhas
    with REGISTRO.open("a", encoding="utf-8") as f:
        agora = datetime.now().isoformat(timespec="seconds")
        for o, n in pares:
            a, b = por_id[o], por_id[n]
            f.write(json.dumps({"quando": agora, "motivo": motivo, "apagada": o in apagadas,
                                "antiga": {k: a.get(k) for k in ("databaseID", "persistentID", "name", "artist",
                                                                  "album", "cloudStatus", "kind", "bitRate",
                                                                  "playedCount", "arquivo")},
                                "nova": {k: b.get(k) for k in ("databaseID", "persistentID", "name", "artist",
                                                                "album", "cloudStatus")}},
                               ensure_ascii=False) + "\n")
    T.msg(f"   {len(apagadas)} faixas antigas removidas da biblioteca"
          + (f"; {len(falhas)} não consegui remover" if falhas else ""))
    return apagadas


# ───────────────────────── aplicar trocas pelo catálogo ─────────────────────────

def job_aplicar(ids):
    an = carregar(ANALISE, {})
    escolhidos = [i for i in an.get("itens", []) if i["id"] in set(ids) and not i.get("feito")]
    if not escolhidos:
        raise RuntimeError("Nada selecionado (ou já aplicado).")
    T.passo(f"Trocando {len(escolhidos)} álbuns…", len(escolhidos))
    for lote in em_lotes(escolhidos, 12):
        aplicar_lote(lote)
        T.feito += len(lote)
        estado = {i["id"]: i for i in lote}
        an = carregar(ANALISE, {})
        for i in an.get("itens", []):
            if i["id"] in estado:
                i.update({k: estado[i["id"]].get(k) for k in ("feito", "pendentes")})
        salvar(ANALISE, an)


def aplicar_lote(lote):
    songs = list(dict.fromkeys(f["new"] for i in lote for f in i["faixas"]))
    T.msg(f"Adicionando {len(songs)} faixas do catálogo à biblioteca "
          f"({', '.join(i['album'] for i in lote[:3])}{'…' if len(lote) > 3 else ''})")
    for ch in em_lotes(songs, 50):
        AM.req("POST", "/v1/me/library", {"ids[songs]": ",".join(ch)}, usuario=True)
    antigos = {f["old"] for i in lote for f in i["faixas"]}
    esperado = {}
    for i in lote:
        for f in i["faixas"]:
            esperado[(i["id"], f["old"])] = (norm(f["novoNome"]), norm(i["catalogo"]["nome"]), f["novoDur"])
    achados, inicio, faixas = {}, time.time(), []
    while True:
        faixas = exportar_biblioteca()
        indice = defaultdict(list)
        for t in faixas:
            if t["cloudStatus"] == "subscription" and t["databaseID"] not in antigos:
                indice[(norm(t["name"]), norm(t["album"]))].append(t)
        for k, (nome, album, dur) in esperado.items():
            if k in achados:
                continue
            cands = [t for t in indice.get((nome, album), []) if abs((t["duration"] or 0) - dur) <= 3]
            if cands:
                achados[k] = max(cands, key=lambda t: t["dateAdded"] or "")["databaseID"]
        if len(achados) == len(esperado) or time.time() - inicio > 600:
            break
        T.msg(f"   aguardando o iCloud: {len(achados)}/{len(esperado)} faixas novas apareceram no Mac")
        time.sleep(20)
    pares = [(old, new) for (_, old), new in achados.items()]
    apagadas = trocar(pares, faixas, "catálogo")
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
            resumo = lambda t: {k: t.get(k) for k in ("databaseID", "cloudStatus", "kind", "bitRate",  # noqa: E731
                                                     "playedCount", "dateAdded")}
            grupos.append({"id": gid, "nome": b[0]["name"], "artista": b[0]["artist"], "album": b[0]["album"],
                           "manter": resumo(b[0]), "remover": [resumo(t) for t in b[1:]]})
    grupos.sort(key=lambda x: (norm(x["artista"]), norm(x["album"]), norm(x["nome"])))
    return grupos


def job_repetidas(ids):
    T.passo("Lendo a biblioteca…")
    faixas = exportar_biblioteca()
    grupos = [g for g in achar_repetidas(faixas) if g["id"] in set(ids)]
    pares = [(r["databaseID"], g["manter"]["databaseID"]) for g in grupos for r in g["remover"]]
    T.passo(f"Removendo {len(pares)} repetidas de {len(grupos)} músicas…")
    trocar(pares, faixas, "repetida")


# ───────────────────────── servidor local ─────────────────────────

def estado():
    return {"configurado": bool(AM.dev), "devToken": AM.dev, "logado": bool(AM.user), "storefront": AM.sf,
            "tarefa": T.estado(), "temAnalise": ANALISE.exists(), "backup": str(BACKUP),
            "config": {k: v for k, v in (carregar(CONFIG, {}) or {}).items()}}


def configurar(cfg):
    caminho = os.path.expanduser(cfg.get("key_path", "").strip())
    if not Path(caminho).is_file():
        raise RuntimeError(f"Não achei o arquivo .p8 em {caminho}")
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
            if caminho == "/api/analise":
                return self._json(carregar(ANALISE, {}) or {})
            if caminho == "/api/repetidas":
                return self._json({"grupos": achar_repetidas(exportar_biblioteca())})
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
            if caminho == "/api/config":
                configurar(corpo)
            elif caminho == "/api/login":
                entrar(corpo["token"])
            elif caminho == "/api/analisar":
                if not AM.user:
                    raise RuntimeError("Entre no Apple Music primeiro.")
                T.iniciar("Análise", job_analisar)
            elif caminho == "/api/aplicar":
                T.iniciar("Troca", job_aplicar, corpo.get("ids") or [])
            elif caminho == "/api/repetidas":
                T.iniciar("Repetidas", job_repetidas, corpo.get("ids") or [])
            else:
                return self._json({"erro": "não encontrado"}, 404)
            self._json(estado())
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._json({"erro": str(e)}, 400)


def iniciar():
    DADOS.mkdir(exist_ok=True)
    cfg = carregar(CONFIG)
    if cfg:
        try:
            AM.dev = gerar_token(cfg["team_id"], cfg["key_id"], cfg["key_path"])
        except Exception as e:  # noqa: BLE001
            print("Chave MusicKit:", e)
    tok = carregar(TOKEN)
    if tok:
        AM.user, AM.sf = tok.get("token"), tok.get("storefront") or "br"
    srv = ThreadingHTTPServer(("127.0.0.1", PORTA), Pagina)
    url = f"http://localhost:{PORTA}"
    print(f"Troca Lossless em {url}  (Ctrl+C para sair)")
    if "--sem-navegador" not in sys.argv:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        AM.salvar_cache(True)


if __name__ == "__main__":
    iniciar()
