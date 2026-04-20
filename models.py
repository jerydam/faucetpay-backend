from __future__ import annotations
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


# ─── Request Bodies ──────────────────────────────────────────────────────────

class CreateChallengeRequest(BaseModel):
    topic: str
    creatorAddress: str
    creatorUsername: str
    stakeAmount: float          # human-readable (e.g. 5.0 USDC)
    tokenSymbol: str = "USDC"
    chainId: int
    isPublic: bool = True
    inviteWallet: Optional[str] = None  # if set → private friend invite


class JoinChallengeRequest(BaseModel):
    walletAddress: str
    username: str
    txHash: str                 # on-chain stake tx the joiner submitted


class RematachRequest(BaseModel):
    requesterWallet: str        # must be one of the two original players


class ClaimRequest(BaseModel):
    code: str
    walletAddress: str


# ─── WebSocket Message Shapes (inbound) ──────────────────────────────────────

class WsReady(BaseModel):
    type: str = "ready"
    walletAddress: str


class WsChat(BaseModel):
    type: str = "chat"
    walletAddress: str
    username: str
    text: str


class WsSubmitAnswer(BaseModel):
    type: str = "submit_answer"
    walletAddress: str
    roundIndex: int
    questionIndex: int
    answerId: str               # "A" | "B" | "C" | "D"
    timeTaken: float            # seconds the player took


# ─── Response / Payload Shapes ────────────────────────────────────────────────

class PlayerState(BaseModel):
    username: str
    points: int = 0
    ready: bool = False
    txVerified: bool = False


class ChallengePublic(BaseModel):
    """Shape returned to clients — rounds_data is STRIPPED of correctId."""
    id: str
    code: str
    topic: str
    creator: str
    creatorName: str
    stake: float
    token: str
    chainId: int
    status: str
    isPublic: bool
    players: Dict[str, PlayerState]

class StakeOfferRequest(BaseModel):
    walletAddress: str
    amount: float     
    
class LobbyEntry(BaseModel):
    code: str
    topic: str
    stakeAmount: float
    tokenSymbol: str
    chainId: int
    creatorUsername: str
    creatorWins: int
    createdAt: str

class UserProfile(BaseModel):
    walletAddress: str
    xAccounts: List[dict] = []
    completedTasks: List[str] = []
    droplistStatus: str = "pending" # pending, eligible, completed
class SyncProfileRequest(BaseModel):
    wallet_address: str
    username: str
    avatar_url: Optional[str] = ""
    email: Optional[str] = ""

class NotificationOut(BaseModel):
    id: str
    type: str
    title: str
    body: str
    data: Optional[Dict[str, Any]] = None
    isRead: bool
    createdAt: str
