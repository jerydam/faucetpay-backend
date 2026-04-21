"""
notifications.py
────────────────
In-app notification system.

Two delivery mechanisms:
  1. DB-backed persistence  → clients poll GET /api/notifications/{wallet}
  2. Live push via WebSocket → if the target wallet has an open /ws/notify/{wallet}
                               connection the message is sent immediately
"""
from __future__ import annotations
import json, logging
from typing import Dict, List, Optional, Any
from fastapi import WebSocket
import asyncpg

logger = logging.getLogger(__name__)


class NType:
    PUBLIC_CHALLENGE = "public_challenge"
    CHALLENGE_INVITE = "challenge_invite"
    PLAYER_JOINED    = "player_joined"
    GAME_STARTING    = "game_starting"
    GAME_OVER        = "game_over"
    REMATCH_REQUEST  = "rematch_request"
    REWARD_AVAILABLE = "reward_available"


class NotificationService:
    def __init__(
        self,
        pool: asyncpg.Pool,
        live_connections: Dict[str, List[WebSocket]],
    ):
        self.pool  = pool
        self.conns = live_connections

    # ─── Core send ───────────────────────────────────────────────────────────

    async def send(
        self,
        recipient: str,
        type_: str,
        title: str,
        body: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Persist notification + push to live socket if connected. Returns notif id."""
        recipient = recipient.lower()

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO notifications (recipient_wallet, type, title, body, data)
                   VALUES ($1, $2, $3, $4, $5)
                   RETURNING id, created_at""",
                recipient, type_, title, body,
                json.dumps(data) if data else None,
            )

        notif_id = str(row["id"])
        payload  = {
            "id":        notif_id,
            "type":      type_,
            "title":     title,
            "body":      body,
            "data":      data,
            "isRead":    False,
            "createdAt": row["created_at"].isoformat(),
        }

        await self._push(recipient, payload)
        return notif_id

    # ─── Broadcast to multiple recipients ────────────────────────────────────

    async def broadcast(
        self,
        recipients: List[str],
        type_: str,
        title: str,
        body: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        for wallet in recipients:
            await self.send(wallet, type_, title, body, data)

    # ─── Convenience helpers ─────────────────────────────────────────────────

    async def notify_public_challenge(
        self,
        code: str,
        topic: str,
        creator: str,
        stake: float,
        token: str,
        creator_username: str = "",
    ) -> None:
        display_name = creator_username or creator[:8]

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT wallet_address FROM players WHERE wallet_address != $1",
                creator.lower(),
            )

        logger.info(
            "notify_public_challenge: code=%s  recipients=%d  creator=%s",
            code, len(rows), creator,
        )

        for row in rows:
            await self.send(
                row["wallet_address"],
                NType.PUBLIC_CHALLENGE,
                "🎯 New Public Challenge",
                f'{display_name} is challenging you on "{topic}" — stake {stake} {token}!',
                {
                    "code":        code,
                    "topic":       topic,
                    "stake":       stake,
                    "token":       token,
                    "creatorName": display_name,
                },
            )

    async def notify_friend_invite(
        self,
        code: str,
        topic: str,
        from_username: str,
        to_wallet: str,
        stake: float,
        token: str,
    ) -> None:
        await self.send(
            to_wallet,
            NType.CHALLENGE_INVITE,
            f"⚔️ {from_username} challenged you!",
            f'You\'ve been invited to a "{topic}" quiz. Stake: {stake} {token}. Use code {code}.',
            {
                "code":        code,
                "topic":       topic,
                "stake":       stake,
                "token":       token,
                "creatorName": from_username,
            },
        )

    async def notify_player_joined(
        self,
        code: str,
        joiner_username: str,
        creator_wallet: str,
    ) -> None:
        await self.send(
            creator_wallet,
            NType.PLAYER_JOINED,
            "👥 Opponent joined!",
            f"{joiner_username} accepted your challenge. Head back to start!",
            {"code": code},
        )

    async def notify_game_starting(self, code: str, wallets: List[str]) -> None:
        for wallet in wallets:
            await self.send(
                wallet,
                NType.GAME_STARTING,
                "🚀 Game is starting!",
                "Both players are ready. The challenge begins now!",
                {"code": code},
            )

    async def notify_game_over(
        self,
        code: str,
        winner_wallet: str,
        loser_wallet: str,
        stake: float,
        token: str,
    ) -> None:
        await self.send(
            winner_wallet,
            NType.REWARD_AVAILABLE,
            "🏆 You won! Claim your reward.",
            f"You beat your opponent! Claim {stake * 2} {token} now.",
            {"code": code},
        )
        await self.send(
            loser_wallet,
            NType.GAME_OVER,
            "😔 Better luck next time",
            "You lost this round. Challenge them to a rematch!",
            {"code": code},
        )

    async def notify_rematch_request(
        self,
        code: str,
        requester_username: str,
        opponent_wallet: str,
    ) -> None:
        await self.send(
            opponent_wallet,
            NType.REMATCH_REQUEST,
            "🔁 Rematch requested!",
            f"{requester_username} wants a rematch. Accept the challenge!",
            {"code": code},
        )

    # ─── DB reads ─────────────────────────────────────────────────────────────

    async def get_unread(self, wallet: str, limit: int = 20) -> list:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, type, title, body, data, is_read, created_at
                   FROM notifications
                   WHERE recipient_wallet=$1
                   ORDER BY created_at DESC
                   LIMIT $2""",
                wallet.lower(), limit,
            )
        return [_row_to_dict(r) for r in rows]

    async def mark_read(self, wallet: str, notif_id: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE notifications SET is_read=TRUE WHERE id=$1 AND recipient_wallet=$2",
                notif_id, wallet.lower(),
            )

    async def mark_all_read(self, wallet: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE notifications SET is_read=TRUE WHERE recipient_wallet=$1",
                wallet.lower(),
            )

    async def unread_count(self, wallet: str) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM notifications WHERE recipient_wallet=$1 AND is_read=FALSE",
                wallet.lower(),
            )

    # ─── Internal push ────────────────────────────────────────────────────────

    async def _push(self, wallet: str, payload: dict) -> None:
        sockets = self.conns.get(wallet, [])
        dead    = []
        for ws in sockets:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            sockets.remove(ws)


def _row_to_dict(row) -> dict:
    d          = dict(row)
    d["id"]        = str(d["id"])
    d["createdAt"] = d.pop("created_at").isoformat()
    d["isRead"]    = d.pop("is_read")
    if d["data"] and isinstance(d["data"], str):
        d["data"] = json.loads(d["data"])
    return d