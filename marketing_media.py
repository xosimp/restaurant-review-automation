"""marketing_media.py — photos for marketing posts.

Instagram *requires* an image and the app's only way to supply one was a text
field asking the owner to paste a public image URL. On a phone. Where the
photo is in the camera roll and there is no URL for it anywhere. That made
Instagram publishing theoretically available and practically unusable, which
is the single reason this module exists.

Where the bytes live. Not on disk: Railway rebuilds the container filesystem
on every deploy, and Meta fetches the image URL at PUBLISH time — which for a
scheduled post is days after the upload — so a file that vanishes on deploy is
a post that fails silently later, with no way to tell it was ever going to.
Not in object storage either: that is a new service, new credentials and a new
failure mode for something the database already does. Photos live in
marketing_media as bytes, which gives them exactly the durability the rest of
a client's data has.

The cost of that choice is row size, so it is paid down here: every upload is
re-encoded and downscaled before it is stored (see `store_image`), which turns
a 4MB iPhone photo into roughly 200-400KB with no visible difference at the
size Instagram serves.
"""
import io
import logging
import secrets

from models import get_conn, DB_PATH

log = logging.getLogger(__name__)

# Instagram's own recommendation is 1080px on the long edge; it downsamples
# anything larger. 1440 leaves headroom for Google Business, which shows
# photos larger, without storing a 12-megapixel original nobody will see.
MAX_EDGE = 1440
JPEG_QUALITY = 86

# The largest photo the app can actually receive. hosted_dashboard caps
# every request body at 5 MB and the iOS app sends base64 (+33%), so the
# 12 MB promised here could never be reached — a bigger photo died on an
# HTML 413 instead of this message (MOD-MKT-14). Both clients downscale
# before uploading (2048 px on the long edge), which keeps a real photo far
# under this.
MAX_UPLOAD_BYTES = int(3.5 * 1024 * 1024)
MAX_UPLOAD_LABEL = "3.5 MB"

# Pixels, read from the file header BEFORE anything is decoded. A 20 KB PNG
# can declare a 13000x13000 canvas, and decoding it cost ~580 MB in the one
# web process (MOD-MKT-13). 50 MP still admits a 48 MP phone photo.
MAX_PIXELS = 50_000_000

# Only what Pillow here can decode. HEIC/HEIF were listed, but no HEIF
# decoder is installed, so a desktop HEIC passed this gate and then failed
# as "didn't open as a photo" (MOD-MKT-14). The iOS app converts to JPEG
# before it uploads, so this only turns a confusing failure into a clear one.
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}


class MediaError(Exception):
    """Something the owner needs to be told, in words they can act on."""


def _new_token() -> str:
    return secrets.token_urlsafe(18)


def store_image(restaurant_id: int, raw: bytes, mime: str = "", db_path: str = DB_PATH) -> dict:
    """Downscale, re-encode as JPEG, store, and return the row.

    Everything comes back out as JPEG regardless of what went in — HEIC
    straight off an iPhone is the common case and Meta will not fetch it.
    """
    if not raw:
        raise MediaError("That file was empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise MediaError(f"That photo is too large — pick one under {MAX_UPLOAD_LABEL}.")
    kind = mime.lower().split(";")[0].strip() if mime else ""
    if kind in ("image/heic", "image/heif"):
        raise MediaError("HEIC photos can't be read here yet — save it as a JPEG and add it again.")
    if kind and kind not in ALLOWED_MIME:
        raise MediaError("That file isn't a photo Cavnar AI can post.")

    try:
        from PIL import Image, ImageOps
    except ImportError as e:  # pragma: no cover - Pillow is a hard dependency
        raise MediaError("Image processing isn't available on this server.") from e

    try:
        # open() reads only the header; nothing is decoded until load().
        img = Image.open(io.BytesIO(raw))
        width, height = img.size
    except Exception as e:
        raise MediaError("That file didn't open as a photo.") from e
    if width * height > MAX_PIXELS:
        raise MediaError("That image is far larger than a photo needs to be. Pick a smaller one.")
    try:
        if img.format == "JPEG":
            # Decode a JPEG at a reduced scale when it is bigger than needed.
            img.draft("RGB", (MAX_EDGE * 2, MAX_EDGE * 2))
        # EXIF orientation, or every photo shot in portrait arrives sideways.
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
    except Exception as e:
        raise MediaError("That file didn't open as a photo.") from e

    img.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    data = buf.getvalue()

    token = _new_token()
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO marketing_media (restaurant_id, token, mime, data, width, height, size_bytes) "
            "VALUES (?,?,?,?,?,?,?)",
            (restaurant_id, token, "image/jpeg", data, img.width, img.height, len(data)),
        )
        conn.commit()
        media_id = cur.lastrowid
    finally:
        conn.close()

    return {"id": media_id, "token": token, "width": img.width,
            "height": img.height, "size_bytes": len(data)}


def get_image(token: str, db_path: str = DB_PATH):
    """(bytes, mime) for the public serving route, or None."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT data, mime FROM marketing_media WHERE token=?", (token,)
        ).fetchone()
    finally:
        conn.close()
    return (row["data"], row["mime"]) if row else None


def media_url(base_url: str, token: str) -> str:
    """The public URL Meta and Google fetch. Deliberately unauthenticated —
    their servers cannot log in — and unguessable instead, which is the same
    trade the staff schedule share links already make."""
    return f"{base_url.rstrip('/')}/m/{token}.jpg"


def get_media_token(media_id: int, restaurant_id: int, db_path: str = DB_PATH):
    """Token for a media row, scoped to its owner so one restaurant can never
    address another's photo by guessing an id."""
    if not media_id:
        return None
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT token FROM marketing_media WHERE id=? AND restaurant_id=?",
            (media_id, restaurant_id),
        ).fetchone()
    finally:
        conn.close()
    return row["token"] if row else None


def list_media(restaurant_id: int, limit: int = 30, db_path: str = DB_PATH) -> list:
    """Recent uploads, so a photo can be reused without re-picking it."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, token, width, height, size_bytes, created_at FROM marketing_media "
            "WHERE restaurant_id=? ORDER BY id DESC LIMIT ?",
            (restaurant_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def delete_media(media_id: int, restaurant_id: int, db_path: str = DB_PATH) -> bool:
    """True only when a photo of this restaurant's was actually deleted. A
    photo a draft or a queued post still needs is kept (the foreign keys on
    both refuse it) and reported as not deleted, not raised."""
    import sqlite3
    conn = get_conn(db_path)
    try:
        n = conn.execute(
            "DELETE FROM marketing_media WHERE id=? AND restaurant_id=?",
            (media_id, restaurant_id),
        ).rowcount
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return False
    finally:
        conn.close()
    return bool(n)


def media_in_use(media_id: int, restaurant_id: int, db_path: str = DB_PATH) -> bool:
    """Whether a draft or a not-yet-published post of this restaurant's
    still points at the photo."""
    conn = get_conn(db_path)
    try:
        return bool(conn.execute(
            "SELECT 1 FROM marketing_scheduled_posts WHERE media_id=? AND restaurant_id=? "
            "AND status IN ('scheduled', 'publishing') "
            "UNION ALL SELECT 1 FROM marketing_drafts WHERE media_id=? AND restaurant_id=? LIMIT 1",
            (media_id, restaurant_id, media_id, restaurant_id)).fetchone())
    except Exception:
        return False
    finally:
        conn.close()


def remove_media(media_id: int, restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """The delete route's answer, with its HTTP status. It said ok=True for a
    photo that was not this restaurant's, or not there at all (MOD-MKT-18),
    and a photo a queued post needed ended in a bare 500 (MOD-A6-media-9)."""
    if media_in_use(media_id, restaurant_id, db_path=db_path):
        return {"ok": False, "status": 409,
                "error": "A scheduled post or a draft still uses this photo. "
                         "Remove it there first, then delete the photo."}
    if delete_media(media_id, restaurant_id, db_path=db_path):
        return {"ok": True, "status": 200}
    if get_media_token(media_id, restaurant_id, db_path=db_path):
        return {"ok": False, "status": 409,
                "error": "A post still uses this photo, so it was kept."}
    return {"ok": False, "status": 404, "error": "That photo isn't in your library."}
