"""Mise API.

Run:  uvicorn server.main:app --host 0.0.0.0 --port 8000
Docs: http://<host>:8000/docs
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import assistant, db
from .models import AdaptRequest, ImportRequest, ImportResponse, QtyAdjust, StatePut
from .scale import SCALE

WEB = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="Mise", version="1.0",
              description="Kitchen instrumentation server. Deterministic maths in "
                          "Python, the model only at the edges.")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


@app.on_event("startup")
def _startup() -> None:
    db.init()


# ── state ───────────────────────────────────────────────────────────────────
@app.get("/api/state")
def get_state() -> dict[str, Any]:
    return db.get_state()


@app.put("/api/state")
def put_state(body: StatePut) -> dict[str, Any]:
    """Whole-state write with optimistic concurrency.

    A stale `rev` means another device wrote since you read. We refuse rather
    than silently overwriting — the client refetches and retries. Cook logs and
    pantry decrements do NOT come through here precisely so they can never be
    lost to this check.
    """
    current = db.rev()
    if body.rev != current:
        raise HTTPException(409, {"error": "stale", "yourRev": body.rev, "currentRev": current})
    if body.settings:
        db.put_settings(body.settings)
    if body.pantry:
        db.upsert_pantry(body.pantry)
    if body.custom:
        db.upsert_recipes(body.custom, builtin=False)
    return {"ok": True, "rev": db.rev()}


@app.post("/api/seed")
def seed(body: dict[str, Any]) -> dict[str, Any]:
    """One-shot import of the browser prototype's localStorage blob."""
    if body.get("pantry"):
        db.upsert_pantry(list(body["pantry"].values()) if isinstance(body["pantry"], dict)
                         else body["pantry"])
    if body.get("custom"):
        db.upsert_recipes(body["custom"], builtin=False)
    if body.get("builtin"):
        db.upsert_recipes(body["builtin"], builtin=True)
    settings = {k: v for k, v in body.items()
                if k in db.DEFAULT_SETTINGS}
    if settings:
        db.put_settings(settings)
    for cook in body.get("log", []):
        db.append_cook(cook)
    return {"ok": True, "rev": db.rev()}


# ── pantry ──────────────────────────────────────────────────────────────────
@app.patch("/api/pantry/{item_id}")
def patch_qty(item_id: str, body: QtyAdjust) -> dict[str, Any]:
    """Atomic. This is what the scale and the intake flow call."""
    out = db.adjust_qty(item_id, body.delta, body.absolute, body.bought, body.expires)
    if out is None:
        raise HTTPException(404, "no such pantry item")
    return out


@app.get("/api/pantry.csv", response_class=PlainTextResponse)
def pantry_csv() -> str:
    st = db.get_state()
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["id", "name", "nl", "cat", "tag", "cls", "qty", "unit", "bought", "expires",
            "low", "price", "pack", "tare", "barcode"]
    w.writerow(cols)
    for it in st["pantry"].values():
        w.writerow([it.get(c, "") for c in cols])
    return buf.getvalue()


@app.get("/api/barcode/{code}")
async def barcode(code: str) -> dict[str, Any]:
    """Known barcode → your pantry item. Unknown → Open Food Facts.

    Returns `net` (grams/ml) when OFF knows the pack size, which is what makes
    the tare table work: tare = gross_you_weigh - net.
    """
    known = db.find_by_barcode(code)
    if known:
        return {"known": True, "item": known}
    url = f"https://world.openfoodfacts.org/api/v2/product/{code}.json"
    try:
        import httpx
        async with httpx.AsyncClient(timeout=12,
                                     headers={"User-Agent": assistant.UA}) as cl:
            r = await cl.get(url)
            data = r.json()
    except Exception as e:
        raise HTTPException(502, f"Open Food Facts unreachable: {type(e).__name__}")
    if data.get("status") != 1:
        return {"known": False, "found": False, "code": code}
    p = data["product"]
    return {"known": False, "found": True, "code": code,
            "name": p.get("product_name") or p.get("generic_name"),
            "brand": p.get("brands"), "categories": p.get("categories"),
            "net": p.get("product_quantity"), "quantity": p.get("quantity"),
            "image": p.get("image_front_small_url")}


# ── cooks ───────────────────────────────────────────────────────────────────
@app.post("/api/cooks")
def post_cook(cook: dict[str, Any]) -> dict[str, Any]:
    """Append-only. Also decrements the pantry atomically, per ingredient."""
    if not cook.get("id") or not cook.get("name"):
        raise HTTPException(422, "cook needs id and name")
    cid = db.append_cook(cook)
    for pid, grams in (cook.get("actual") or {}).items():
        try:
            if float(grams) > 0:
                db.adjust_qty(pid, delta=-float(grams))
        except Exception:
            pass
    return {"ok": True, "cookId": cid, "rev": db.rev()}


@app.get("/api/cooks.csv", response_class=PlainTextResponse)
def cooks_csv() -> str:
    st = db.get_state()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "recipe", "mode", "theme", "scale_factor", "mae_pct",
                "rating", "total_s", "break_s"])
    for l in st["log"]:
        rt = l.get("rating") or {}
        overall = ""
        if rt:
            overall = round((rt.get("taste", 0) + rt.get("texture", 0) + rt.get("again", 0)) / 3, 2)
        w.writerow([l["date"], l["name"], l["mode"], l.get("theme") or "", l.get("factor"),
                    l.get("mae"), overall, round((l.get("totalMs") or 0) / 1000),
                    round((l.get("breakMs") or 0) / 1000)])
    return buf.getvalue()


# ── recipes ─────────────────────────────────────────────────────────────────
@app.post("/api/recipes")
def post_recipe(r: dict[str, Any]) -> dict[str, Any]:
    if not r.get("id") or not r.get("stages"):
        raise HTTPException(422, "recipe needs id and stages")
    db.upsert_recipes([r], builtin=bool(r.get("builtin")))
    return {"ok": True, "rev": db.rev()}


@app.delete("/api/recipes/{rid}")
def del_recipe(rid: str) -> dict[str, Any]:
    db.delete_recipe(rid)
    return {"ok": True, "rev": db.rev()}


# ── assistant ───────────────────────────────────────────────────────────────
@app.post("/api/import", response_model=ImportResponse)
async def import_recipe(body: ImportRequest) -> ImportResponse:
    if not body.url and not body.text:
        raise HTTPException(422, "give me a url or some text")
    try:
        draft, provenance, warnings = await assistant.build_draft(body.url, body.text, body.hint)
    except ValueError as e:
        raise HTTPException(422, str(e))
    pantry = db.get_state()["pantry"]
    matched, unmatched = assistant.resolve(draft, pantry)
    if unmatched:
        warnings.append(f"{len(unmatched)} ingredient(s) didn't match anything you own — "
                        "map them by hand or they'll be dropped.")
    return ImportResponse(draft=draft, provenance=provenance, matched=matched,
                          unmatched=unmatched, warnings=warnings)


@app.post("/api/assist/adapt")
async def assist_adapt(body: AdaptRequest) -> dict[str, Any]:
    pantry = db.get_state()["pantry"]
    names = [p["name"] for p in pantry.values() if p.get("qty", 0) > 0]
    try:
        return await assistant.adapt(body.recipe, body.instruction, names)
    except RuntimeError:
        raise HTTPException(503, "No model API key configured on the server — set one in .env.")
    except Exception as e:
        raise HTTPException(502, f"model call failed: {type(e).__name__}")


@app.get("/api/assist/health")
def assist_health() -> dict[str, Any]:
    import os
    import shutil
    prov = assistant.which_provider()
    return {"model_key": prov is not None,
            "provider": prov[0].replace("_API_KEY", "").title() if prov else None,
            "model": prov[3] if prov else None,
            "yt_dlp": shutil.which("yt-dlp") is not None,
            "tiers": ["json-ld", "youtube-transcript", "page-text", "pasted-text", "regex"]}


# ── scale ───────────────────────────────────────────────────────────────────
@app.get("/api/scale")
def scale_status() -> dict[str, Any]:
    return SCALE.status()


@app.post("/api/scale/tare")
def scale_tare() -> dict[str, Any]:
    return {"offset": SCALE.tare(), **SCALE.status()}


@app.post("/api/scale/calibrate")
def scale_cal(body: dict[str, float]) -> dict[str, Any]:
    try:
        s = SCALE.calibrate(float(body.get("grams", 0)))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"scale": s, **SCALE.status()}


@app.post("/api/scale/sim")
def scale_sim(body: dict[str, float]) -> dict[str, Any]:
    SCALE.sim_set(float(body.get("grams", 0)))
    return SCALE.status()


@app.websocket("/ws/scale")
async def ws_scale(ws: WebSocket) -> None:
    """Live weight for the cook screen. ~10 Hz is plenty for a human pouring."""
    await ws.accept()
    ingredient = None
    try:
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=0.001)
                d = json.loads(msg)
                if d.get("op") == "tare":
                    SCALE.tare()
                elif d.get("op") == "track":
                    ingredient = d.get("ingredient")
                elif d.get("op") == "sim":
                    SCALE.sim_set(float(d.get("grams", 0)))
            except (asyncio.TimeoutError, json.JSONDecodeError):
                pass
            st = SCALE.status()
            if ingredient:
                db.log_scale(int(time.time() * 1000), st["grams"], ingredient)
            await ws.send_json(st)
            await asyncio.sleep(0.1)
    except WebSocketDisconnect:
        return


# ── static frontend ─────────────────────────────────────────────────────────
@app.get("/")
def root() -> FileResponse:
    return FileResponse(WEB / "index.html")


if WEB.exists():
    app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
