"""Upload pro YouTube via Data API v3, usando o token OAuth gerado por
scripts/auth_youtube.py (um token.json por canal)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from .config import ChannelConfig

log = logging.getLogger("upload")

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]

# playlist_id de cada canal, criada 1x e reaproveitada (evita duplicar
# playlist a cada upload e evita ter que fazer playlists().list toda vez).
PLAYLIST_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "playlists.json"


def _load_credentials(token_file: Path) -> Credentials:
    if not token_file.exists():
        raise FileNotFoundError(
            f"Token não encontrado: {token_file}. Rode "
            f"`python3 scripts/auth_youtube.py --channel <nome>` primeiro."
        )
    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_file.write_text(creds.to_json())
    return creds


def upload_video(
    channel: ChannelConfig,
    video_path: Path,
    title: str,
    description: str,
    tags: list[str],
    thumbnail_path: Path | None = None,
    publish_at: str | None = None,  # ISO 8601 UTC, ex: "2026-09-08T12:00:00Z"
) -> str:
    """Sobe o vídeo e devolve o videoId. Cota: ~1600 unidades por upload
    (limite padrão diário: 10000 unidades ≈ 6 uploads/dia por projeto)."""
    creds = _load_credentials(channel.token_file)
    youtube = build("youtube", "v3", credentials=creds)

    status = {"privacyStatus": channel.upload_privacy, "selfDeclaredMadeForKids": False}
    if publish_at:
        status["privacyStatus"] = "private"
        status["publishAt"] = publish_at

    body = {
        "snippet": {
            "title": title[:100],
            "description": description,
            "tags": tags,
            "categoryId": "24",  # Entertainment
        },
        "status": status,
    }

    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status_progress, response = request.next_chunk()
        if status_progress:
            log.info("upload %s: %d%%", video_path.name, int(status_progress.progress() * 100))

    video_id = response["id"]
    log.info("vídeo enviado: https://youtu.be/%s", video_id)

    if thumbnail_path and thumbnail_path.exists():
        try:
            youtube.thumbnails().set(
                videoId=video_id, media_body=MediaFileUpload(str(thumbnail_path))
            ).execute()
        except Exception as exc:
            # Canal sem verificação de telefone não pode setar thumbnail
            # customizada via API — não deve derrubar o upload em si.
            log.warning("não consegui setar a thumbnail de %s: %s", video_id, exc)

    try:
        _add_to_channel_playlist(youtube, channel, video_id)
    except Exception as exc:
        # playlist é bônus (ajuda tempo de sessão) — nunca derruba o upload.
        log.warning("não consegui adicionar %s à playlist: %s", video_id, exc)

    return video_id


def _load_playlist_state() -> dict:
    if PLAYLIST_STATE_FILE.exists():
        return json.loads(PLAYLIST_STATE_FILE.read_text())
    return {}


def _save_playlist_state(state: dict) -> None:
    PLAYLIST_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PLAYLIST_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _get_or_create_playlist(youtube, channel: ChannelConfig) -> str:
    """1 playlist por canal, com todos os uploads (curto + longo) — mantém
    quem assiste vendo mais vídeos seus em sequência (tempo de sessão é
    sinal real de recomendação do YouTube). Criada 1x, id salvo em
    data/playlists.json pra nunca duplicar."""
    state = _load_playlist_state()
    cached = state.get(channel.name)
    if cached:
        return cached

    body = {
        "snippet": {
            "title": f"{channel.channel_title} — Todos os vídeos",
            "description": f"Todos os vídeos do canal {channel.channel_title}.",
        },
        "status": {"privacyStatus": "public"},
    }
    response = youtube.playlists().insert(part="snippet,status", body=body).execute()
    playlist_id = response["id"]
    state[channel.name] = playlist_id
    _save_playlist_state(state)
    log.info("playlist criada pro canal %s: %s", channel.name, playlist_id)
    return playlist_id


def _add_to_channel_playlist(youtube, channel: ChannelConfig, video_id: str) -> None:
    playlist_id = _get_or_create_playlist(youtube, channel)
    youtube.playlistItems().insert(
        part="snippet",
        body={
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
            }
        },
    ).execute()
