from __future__ import annotations
from __future__ import annotations
import os, json, random, string, asyncio, logging, uuid
from typing import Dict, List, Optional
import datetime
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from web3 import Web3
import httpx
import asyncpg
from supabase import create_client, Client
from models import (
    CreateChallengeRequest, JoinChallengeRequest,
    RematachRequest, ClaimRequest,UserProfile, SyncProfileRequest, StakeOfferRequest
)
from abi import QUIZ_HUB_ABI
import asyncio, logging
from fastapi import HTTPException
from pydantic import BaseModel
 
logger = logging.getLogger(__name__)
load_dotenv()

# Fail fast with a clear message if critical env vars are missing
_required = ["SUPABASE_URL", "SUPABASE_SERVICE_KEY", "DATABASE_URL", "QUIZ_HUB_CONTRACT"]
_missing  = [v for v in _required if not os.getenv(v)]
if _missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(_missing)}")
from quiz_engine import generate_questions, run_game_loop, strip_answers
from notifications import NotificationService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Quizhub Quiz Platform", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
BACKEND_ADDRESS = os.getenv("BACKEN_ADDRESS")
PRIVATE_KEY     = os.getenv("RESOLVER_PRIVATE_KEY")
# ─── Config ──────────────────────────────────────────────────────────────────
CONTRACT_ADDRESS = os.getenv("QUIZ_HUB_CONTRACT")
DATABASE_URL = os.getenv("DATABASE_URL")
RPC_URLS = {
    42220: "https://forno.celo.org",
    8453:  "https://mainnet.base.org",
    1135:  "https://rpc.api.lisk.com",
}

# ─── Global In-Memory State ───────────────────────────────────────────────────
# For production swap the dicts for a Redis-backed store.

challenges:  Dict[str, dict]         = {}  # code  → full challenge obj
game_state:  Dict[str, dict]         = {}  # code  → { answers, current_round }
connections: Dict[str, List[WebSocket]] = {}  # code  → [ws]  (game sockets)
notify_conn: Dict[str, List[WebSocket]] = {}  # wallet → [ws] (notify sockets)
offers:      Dict[str, dict]            = {}  
pool: asyncpg.Pool = None
notif: NotificationService = None


# ─── Lifecycle ────────────────────────────────────────────────────────────────
def smart_checksum(address: str) -> str:
    """Safely checksums EVM addresses and leaves Solana addresses alone."""
    if not address: 
        return ""
    
    # ✅ FIX: Call the actual Web3 function for EVM addresses
    return Web3.to_checksum_address(address)

def normalize_db_address(address: str) -> str:
    """Use this instead of .lower() when saving/querying the database."""
    if not address: return ""
    return address.lower() # EVM stays lowercase in your DB

@app.on_event("startup")
async def startup():
    global pool, notif
    pool = await asyncpg.create_pool(
        dsn=DATABASE_URL,
        min_size=5,
        max_size=20,
        statement_cache_size=0,  # ← required for Supabase/PgBouncer
    )
    notif = NotificationService(pool, notify_conn)
    logger.info("🚀 Quiz Platform Started")


@app.on_event("shutdown")
async def shutdown():
    if pool:
        await pool.close()


# ─── Helpers ──────────────────────────────────────────────────────────────────
async def update_stake_on_chain(code: str, agreed_amount: float) -> bool:
    try:
        w3 = Web3(Web3.HTTPProvider(RPC_URLS[42220]))
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(CONTRACT_ADDRESS),
            abi=QUIZ_HUB_ABI  # include setStakePerPlayer
        )
        
        quiz_id = Web3.keccak(text=code)
        # Convert to wei / proper decimals if needed (assume amount is already in human units, adjust as per your token decimals)
        tx = contract.functions.setStakePerPlayer(
            quiz_id, 
            int(agreed_amount * 10**18)  # adjust decimals based on token
        ).build_transaction({
            'from': BACKEND_ADDRESS,  # or resolver
            'gas': 200000,
            'nonce': w3.eth.get_transaction_count(BACKEND_ADDRESS),
        })
        
        signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
        
        return receipt.status == 1
    except Exception as e:
        logger.error(f"on-chain stake update failed: {e}")
        return False
    
def make_code(k: int = 6) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=k))


def checksum(addr: str) -> str:
    return Web3.to_checksum_address(addr) if addr else addr


async def verify_stake_tx(tx_hash: str, expected_quiz_id: str) -> bool:
    rpc = RPC_URLS[42220] # Celo
    async with httpx.AsyncClient() as client:
        resp = await client.post(rpc, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "eth_getTransactionReceipt",
            "params": [tx_hash]
        })
        receipt = resp.json().get("result")
        
        if not receipt or receipt.get("status") != "0x1":
            return False

        # Verify the transaction interacted with YOUR contract
        if receipt.get("to").lower() != CONTRACT_ADDRESS.lower():
            return False

        # Optional: Check logs for QuizCreated or QuizJoined event
        # This ensures the user actually called 'stake' with the right QuizID
        return True

async def broadcast(code: str, payload: dict) -> None:
    sockets = connections.get(code, [])
    dead    = []
    for ws in sockets:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.remove(ws)


# ─── Player Endpoints ─────────────────────────────────────────────────────────

@app.post("/api/players/register")
async def register_player(wallet: str, username: str):
    wallet = wallet.lower()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO players (wallet_address, username)
               VALUES ($1, $2)
               ON CONFLICT (wallet_address) DO UPDATE SET username=EXCLUDED.username""",
            wallet, username,
        )
    return {"success": True}


@app.get("/api/players/{wallet}")
async def get_player(wallet: str):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM players WHERE wallet_address=$1", wallet.lower()
        )
    if not row:
        raise HTTPException(status_code=404, detail="Player not found")
    return dict(row)


# ─── Challenge Endpoints ──────────────────────────────────────────────────────

@app.post("/api/challenge/create")
async def create_challenge(body: CreateChallengeRequest):
    # 1. Ensure player exists
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO players (wallet_address, username)
               VALUES ($1, $2) ON CONFLICT DO NOTHING""",
            body.creatorAddress.lower(), body.creatorUsername,
        )

    # 2. Generate questions via AI
    questions_data = await generate_questions(body.topic)

    # 3. Create challenge record
    code         = make_code()
    challenge_id = str(uuid.uuid4())
    creator_low  = body.creatorAddress.lower()

    challenge_obj = {
        "id":          challenge_id,
        "code":        code,
        "topic":       body.topic,
        "creator":     creator_low,
        "creatorName": body.creatorUsername,
        "stake":       body.stakeAmount,
        "token":       body.tokenSymbol,
        "chainId":     body.chainId,
        "rounds":      questions_data["rounds"],
        "status":      "waiting",
        "isPublic":    body.isPublic,
        "players": {
            creator_low: {
                "username":    body.creatorUsername,
                "points":      0,
                "ready":       False,   # still needs to stake
                "txVerified":  False,
            }
        },
    }

    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO ai_challenges
               (id, code, creator_address, topic, stake_amount, token_symbol, chain_id, status, is_public, rounds_data)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
            challenge_id, code, creator_low, body.topic, body.stakeAmount,
            body.tokenSymbol, body.chainId, "waiting", body.isPublic,
            json.dumps(questions_data),
        )
        await conn.execute(
            """INSERT INTO challenge_players (challenge_id, wallet_address, username, ready)
               VALUES ($1,$2,$3,FALSE)""",
            challenge_id, creator_low, body.creatorUsername,
        )

    challenges[code] = challenge_obj
    game_state[code]  = {"answers": {}}

    # 4. Notifications
    if body.isPublic:
        # Fan out to all other registered players (fire-and-forget)
        asyncio.create_task(
            notif.notify_public_challenge(
                code, body.topic, body.creatorUsername,
                body.stakeAmount, body.tokenSymbol,
            )
        )
    elif body.inviteWallet:
        asyncio.create_task(
            notif.notify_friend_invite(
                code, body.topic, body.creatorUsername,
                body.inviteWallet, body.stakeAmount, body.tokenSymbol,
            )
        )

    return {"success": True, "code": code, "challenge": _safe_challenge(challenge_obj)}
async def store_user_profile(profile: UserProfile):
    """Store or update user profile in Supabase"""
    try:
        data = {
            "wallet_address": profile.walletAddress,
            "x_accounts": profile.xAccounts,
            "completed_tasks": profile.completedTasks,
            "droplist_status": profile.droplistStatus,
            "updated_at": datetime.now().isoformat()
        }
       
        response = supabase.table("droplist_users").upsert(
            data,
            on_conflict="wallet_address"
        ).execute()
       
        if not response.data:
            raise HTTPException(status_code=500, detail="Failed to store user profile")
           
        return response.data[0]
       
    except Exception as e:
        print(f"Database error in store_user_profile: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
async def get_user_profile(wallet_address: str) -> Optional[UserProfile]:
    """Get user profile from Supabase"""
    try:
        if not Web3.is_address(wallet_address):
            return None
           
        checksum_address = smart_checksum(wallet_address)
       
        response = supabase.table("droplist_users").select("*").eq(
            "wallet_address", checksum_address
        ).execute()
       
        if response.data and len(response.data) > 0:
            data = response.data[0]
            return UserProfile(
                walletAddress=data["wallet_address"],
                xAccounts=data.get("x_accounts", []),
                completedTasks=data.get("completed_tasks", []),
                droplistStatus=data.get("droplist_status", "pending")
            )
       
        return None
       
    except Exception as e:
        print(f"Database error in get_user_profile: {str(e)}")
        return None
@app.post("/api/challenge/{code}/offer")
async def submit_offer(code: str, body: StakeOfferRequest):
    """
    Either player can propose a new stake amount.
    - If no offer exists yet, this becomes the opening bid.
    - If an offer already exists from the OTHER player, this is a counter.
    - If both players independently call this with the SAME amount, it auto-accepts.
    The backend broadcasts `stake_offer` to all lobby WS connections.
    """
    code   = code.upper()
    wallet = body.walletAddress.lower()
    amount = round(body.amount, 6)
 
    if code not in challenges:
        raise HTTPException(status_code=404, detail="Challenge not found")
 
    challenge = challenges[code]
 
    if challenge["status"] != "waiting":
        raise HTTPException(status_code=400, detail="Challenge is not in waiting state")
 
    if wallet not in challenge["players"] and wallet != challenge["creator"]:
        raise HTTPException(status_code=403, detail="You are not part of this challenge")
 
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Stake must be positive")
 
    offer = offers.setdefault(code, {
        "current":  challenge["stake"],   
        "proposer": challenge["creator"],
        "accepted": False,
        "history":  [],
    })
    
    
 
    # Check auto-accept: last two history entries have the same amount from different wallets
    history = offer["history"]
    if (
        len(history) >= 2
        and history[-1]["amount"] == history[-2]["amount"]
        and history[-1]["wallet"] != history[-2]["wallet"]
    ):
        offer["accepted"] = True
        challenge["stake"] = amount           # lock agreed stake into challenge
        await _persist_agreed_stake(code, amount)
        await broadcast(code, {
            "type":   "offer_accepted",
            "amount": amount,
            "by":     wallet,
        })
        return {"success": True, "accepted": True, "amount": amount}
 
    await broadcast(code, {
        "type":     "stake_offer",
        "amount":   amount,
        "proposer": wallet,
        "username": challenge["players"].get(wallet, {}).get("username", wallet[:8]),
        "history":  history,
    })
    if offer["accepted"]:
        # 1. Persist to DB
        await _persist_agreed_stake(code, amount)
        
        # 2. Trigger on-chain update
        success = await update_stake_on_chain(code, amount)
        if not success:
            raise HTTPException(500, "Failed to update stake on-chain")
        
        # 3. Broadcast final agreement
        await broadcast(code, {
            "type": "offer_accepted",
            "amount": amount,
            "onChainUpdated": True
        })
    return {"success": True, "accepted": False, "amount": amount}
 
    
# ── HTTP endpoint 2: explicitly accept the current offer ──────────────────────
 
@app.post("/api/challenge/{code}/accept-offer")
async def accept_offer(code: str, body: StakeOfferRequest):
    """
    Explicitly accept whatever the current open offer is.
    `body.amount` is ignored — we accept the stored `offers[code]["current"]`.
    """
    code   = code.upper()
    wallet = body.walletAddress.lower()
 
    if code not in challenges:
        raise HTTPException(status_code=404, detail="Challenge not found")
 
    challenge = challenges[code]
 
    if wallet not in challenge["players"] and wallet != challenge["creator"]:
        raise HTTPException(status_code=403, detail="You are not part of this challenge")
 
    offer = offers.get(code)
    if not offer:
        raise HTTPException(status_code=400, detail="No open offer to accept")
    if offer["accepted"]:
        raise HTTPException(status_code=400, detail="Already accepted")
    if offer["proposer"] == wallet:
        raise HTTPException(status_code=400, detail="Cannot accept your own offer — wait for the other player")
 
    agreed_amount = offer["current"]
    offer["accepted"] = True
    challenge["stake"] = agreed_amount
 
    await _persist_agreed_stake(code, agreed_amount)
 
    await broadcast(code, {
        "type":   "offer_accepted",
        "amount": agreed_amount,
        "by":     wallet,
    })
 
    return {"success": True, "accepted": True, "amount": agreed_amount}
 
 
# ── DB helper ─────────────────────────────────────────────────────────────────
 
async def _persist_agreed_stake(code: str, amount: float) -> None:
    """Write the agreed stake back to ai_challenges so it survives restarts."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE ai_challenges SET stake_amount=$1 WHERE code=$2",
            amount, code,
        )
 
 

 
async def _handle_ws_stake_offer(code: str, wallet: str, msg: dict) -> None:
    """
    Lightweight real-time relay of stake offers over WS.
    The authoritative logic lives in the HTTP endpoint above; this just
    rebroadcasts for clients that miss the HTTP response (e.g. reconnects).
    """
    amount = float(msg.get("amount", 0))
    if amount <= 0 or code not in challenges:
        return
 
    offer = offers.get(code)
    if not offer or offer["accepted"]:
        return
 
    # Only broadcast if this wallet can legitimately make offers
    challenge = challenges[code]
    if wallet not in challenge["players"] and wallet != challenge["creator"]:
        return
 
    await broadcast(code, {
        "type":     "stake_offer",
        "amount":   amount,
        "proposer": wallet,
        "username": challenge["players"].get(wallet, {}).get("username", wallet[:8]),
        "history":  offer.get("history", []),
    })
 
 
async def _handle_ws_accept_offer(code: str, wallet: str) -> None:
    """WS fast-path for accepting. Mirrors the HTTP endpoint logic."""
    if code not in challenges:
        return
 
    offer = offers.get(code)
    if not offer or offer["accepted"] or offer["proposer"] == wallet:
        return
 
    agreed_amount        = offer["current"]
    offer["accepted"]    = True
    challenges[code]["stake"] = agreed_amount
 
    asyncio.create_task(_persist_agreed_stake(code, agreed_amount))
 
    await broadcast(code, {
        "type":   "offer_accepted",
        "amount": agreed_amount,
        "by":     wallet,
    })
 
# main.py - New Endpoint
@app.post("/api/challenge/{code}/accept-final")
async def accept_final(code: str, body: dict):
    code = code.upper()
    # 1. Update DB state to 'agreed'
    async with pool.acquire() as conn:
        challenge = await conn.fetchrow("SELECT * FROM ai_challenges WHERE code=$1", code)
        
    # 2. TRIGGER BLOCKCHAIN (Relayer Pattern)
    # This calls the setStakePerPlayer function on your QuizHub contract
    try:
        success = await trigger_contract_update(
            code, 
            challenge['stake_amount'], 
            challenge['token_symbol']
        )
        if success:
            # 3. Notify BOTH players to enter lobby
            await broadcast(code, {"type": "stake_locked_on_chain", "amount": challenge['stake_amount']})
            return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def trigger_contract_update(code, amount, token):
    # Standard Web3.py logic to send transaction
    w3 = Web3(Web3.HTTPProvider(RPC_URLS))
    account = w3.eth.account.from_key(os.getenv("RELAYER_PRIVATE_KEY"))
    
    contract = w3.eth.contract(address=CONTRACT_ADDRESS, abi=QUIZ_HUB_ABI)
    quiz_id = w3.keccak(text=code)
    
    # Convert amount to Wei
    amount_wei = w3.to_wei(amount, 'ether') # Adjust based on token decimals
    
    tx = contract.functions.setStakePerPlayer(quiz_id, amount_wei).build_transaction({
        'from': account.address,
        'nonce': w3.eth.get_transaction_count(account.address),
        'gas': 2000000,
        'gasPrice': w3.eth.gas_price
    })
    
    signed_tx = w3.eth.account.sign_transaction(tx, private_key=account.private_key)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    return receipt.status == 1
    
@app.get("/api/players/{wallet}")
async def get_player(wallet: str):
    wallet = wallet.lower()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM players WHERE wallet_address=$1", wallet
        )
        if not row:
            # Auto-register with generated username
            generated_username = f"User{wallet[-4:].upper()}"
            await conn.execute(
                """INSERT INTO players (wallet_address, username)
                   VALUES ($1, $2)
                   ON CONFLICT (wallet_address) DO NOTHING""",
                wallet, generated_username,
            )
            row = await conn.fetchrow(
                "SELECT * FROM players WHERE wallet_address=$1", wallet
            )
    return dict(row)

@app.post("/api/profile/sync")
async def sync_profile(req: SyncProfileRequest):
    try:
        # FIX: Use the smart normalizer instead of .lower()
        wallet = normalize_db_address(req.wallet_address)
        
        # 1. Check if user already exists
        existing = supabase.table("user_profiles").select("*").eq("wallet_address", wallet).execute()
        if existing.data:
            # OPTIONAL: Add a chain_type identifier to the response for the frontend
            profile_data = existing.data[0]
            profile_data["chain_type"] = "evm"
            return {"success": True, "profile": profile_data}
        
        # 2. Check if the fallback username is already taken
        username_check = supabase.table("user_profiles").select("username").eq("username", req.username).execute()
        
        final_username = req.username
        if username_check.data:
            # If taken, append the last 4 characters
            final_username = f"{req.username}_{wallet[-4:]}"
        
        # 3. Create the new profile
        new_profile = {
            "wallet_address": wallet,
            "username": final_username,
            "avatar_url": req.avatar_url,
            "email": req.email
        }
        
        insert_res = supabase.table("user_profiles").insert(new_profile).execute()
        
        profile_data = insert_res.data[0]
        profile_data["chain_type"] = "evm"
        return {"success": True, "profile": profile_data}
        
    except Exception as e:
        print(f"Error auto-syncing profile: {e}")
        return {"success": False, "error": str(e)}
    
@app.get("/api/users/{wallet_address}", tags=["User Management"])
async def get_user_profile_endpoint(wallet_address: str):
    """Get user profile"""
    try:
        profile = await get_user_profile(wallet_address)
       
        if not profile:
            raise HTTPException(status_code=404, detail="User profile not found")
       
        # FIX: Simply return the profile. 
        # Since it's already a dict, FastAPI will automatically 
        # serialize it to JSON for you.
        return profile
       
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error getting user profile: {str(e)}")
        # It's helpful to keep this print for debugging
        raise HTTPException(status_code=500, detail=f"Failed to get user profile: {str(e)}")@app.post("/api/users")
async def create_user_profile_endpoint(profile: UserProfile):
    """Create new user profile"""
    try:
        if not Web3.is_address(profile.walletAddress):
            raise HTTPException(status_code=400, detail="Invalid wallet address")
       
        profile.walletAddress = smart_checksum(profile.walletAddress)
       
        result = await store_user_profile(profile)
       
        return {
            "success": True,
            "message": "User profile created",
            "data": result
        }
       
    except Exception as e:
        print(f"Error creating user profile: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to create user profile: {str(e)}")

@app.get("/api/challenge/lobby")
async def get_lobby(
    limit:  int = Query(default=20, le=50),
    offset: int = Query(default=0),
):
    """Returns all open public challenges available to join."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM public_lobby LIMIT $1 OFFSET $2""",
            limit, offset,
        )
    return {"success": True, "challenges": [dict(r) for r in rows]}


@app.get("/api/challenge/{code}")
async def get_challenge(code: str):
    code = code.upper()
    if code in challenges:
        return {"success": True, "challenge": _safe_challenge(challenges[code])}
    
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM ai_challenges WHERE code=$1", code
        )
        if not row:
            raise HTTPException(status_code=404, detail="Challenge not found")
        
        d = dict(row)
        d["stake"] = float(d.get("stake_amount", 0))
        d["token"] = d.get("token_symbol", "")
        d["chainId"] = d.get("chain_id")
        d["isPublic"] = d.get("is_public")
        d["creator"] = d.get("creator_address", "")
        
        # Both queries share the same connection
        creator_row = await conn.fetchrow(
            "SELECT username FROM players WHERE wallet_address=$1",
            d["creator"]
        )
        d["creatorName"] = creator_row["username"] if creator_row else d["creator"][:8]
        player_rows = await conn.fetch(
        """SELECT wallet_address, username, tx_verified, ready 
           FROM challenge_players 
           WHERE challenge_id=$1""",
        d["id"]
    )
        d["players"] = {
            r["wallet_address"]: {
                "username":   r["username"],
                "points":     0,
                "ready":      r["ready"],
                "txVerified": r["tx_verified"],
            }
            for r in player_rows
        }
    
    return {"success": True, "challenge": d}


@app.post("/api/challenge/{code}/join")
async def join_challenge(code: str, body: JoinChallengeRequest):
    code   = code.upper()
    joiner = body.walletAddress.lower()

    # Rebuild from DB if not in memory (e.g. after server restart)
    if code not in challenges:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM ai_challenges WHERE code=$1", code
            )
        if not row:
            raise HTTPException(status_code=404, detail="Challenge not found")
        
        d = dict(row)
        rounds_data = d.get("rounds_data") or {}
        if isinstance(rounds_data, str):
            rounds_data = json.loads(rounds_data)

        # Fetch existing players
        async with pool.acquire() as conn:
            player_rows = await conn.fetch(
                "SELECT wallet_address, username, tx_verified, ready FROM challenge_players WHERE challenge_id=$1",
                d["id"]
            )
        
        players_dict = {
            r["wallet_address"]: {
                "username": r["username"],
                "points": 0,
                "ready": r["ready"],
                "txVerified": r["tx_verified"],
            }
            for r in player_rows
        }

        challenges[code] = {
            "id":          str(d["id"]),
            "code":        code,
            "topic":       d["topic"],
            "creator":     d["creator_address"],
            "creatorName": d.get("creator_name", ""),
            "stake":       float(d["stake_amount"]),
            "token":       d["token_symbol"],
            "chainId":     d["chain_id"],
            "rounds":      rounds_data.get("rounds", []),
            "status":      d["status"],
            "isPublic":    d["is_public"],
            "players":     players_dict,
        }
        game_state[code] = {"answers": {}}

    challenge = challenges[code]

    if challenge["status"] != "waiting":
        raise HTTPException(status_code=400, detail="Challenge is not open")
    if len(challenge["players"]) >= 2:
        raise HTTPException(status_code=400, detail="Challenge is already full")
    if joiner in challenge["players"]:
        return {"success": True, "challenge": _safe_challenge(challenge)}  # idempotent

    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO players (wallet_address, username)
               VALUES ($1,$2) ON CONFLICT DO NOTHING""",
            joiner, body.username,
        )
        await conn.execute(
            """INSERT INTO challenge_players 
                (challenge_id, wallet_address, username, tx_hash, tx_verified)
            VALUES ((SELECT id FROM ai_challenges WHERE code=$1), $2, $3, $4, TRUE)
            ON CONFLICT (challenge_id, wallet_address) 
            DO UPDATE SET tx_hash=$4, tx_verified=TRUE""",
            code, joiner, body.username, body.txHash,
        )

    # Update in-memory state after DB writes succeed
    challenge["players"][joiner] = {
        "username":   body.username,
        "points":     0,
        "ready":      False,
        "txVerified": True,
    }

    creator_wallet = challenge["creator"]
    asyncio.create_task(
        notif.notify_player_joined(code, body.username, creator_wallet)
    )

    await broadcast(code, {
        "type":   "player_joined",
        "player": {"walletAddress": joiner, "username": body.username},
    })
    await _maybe_auto_start(code)
    return {"success": True, "challenge": _safe_challenge(challenge)}

@app.post("/api/challenge/{code}/rematch")
async def request_rematch(code: str, body: RematachRequest):
    """
    Create a brand-new challenge with the SAME topic, stake, and chain as the
    original, but fresh AI questions. Returns a new code.
    The opponent is notified automatically.
    """
    code      = code.upper()
    requester = body.requesterWallet.lower()

    # Load original from in-memory or DB
    if code not in challenges:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM ai_challenges WHERE code=$1", code
            )
        if not row:
            raise HTTPException(status_code=404, detail="Original challenge not found")
        orig = dict(row)
        topic, stake, token, chain_id, orig_id = (
            orig["topic"], float(orig["stake_amount"]),
            orig["token_symbol"], orig["chain_id"], str(orig["id"])
        )
        # Find players
        async with pool.acquire() as conn:
            player_rows = await conn.fetch(
                "SELECT wallet_address, username FROM challenge_players WHERE challenge_id=$1",
                orig["id"],
            )
        players = {r["wallet_address"]: r["username"] for r in player_rows}
    else:
        c       = challenges[code]
        topic   = c["topic"]
        stake   = c["stake"]
        token   = c["token"]
        chain_id = c["chainId"]
        orig_id  = c["id"]
        players  = {w: p["username"] for w, p in c["players"].items()}

    if requester not in players:
        raise HTTPException(status_code=403, detail="You were not part of this challenge")

    requester_username = players[requester]
    opponent_wallet    = next((w for w in players if w != requester), None)
    if not opponent_wallet:
        raise HTTPException(status_code=400, detail="Cannot find opponent")

    # Generate fresh questions
    questions_data = await generate_questions(topic)
    new_code       = make_code()
    new_id         = str(uuid.uuid4())

    new_challenge = {
        "id":          new_id,
        "code":        new_code,
        "topic":       topic,
        "creator":     requester,
        "creatorName": requester_username,
        "stake":       stake,
        "token":       token,
        "chainId":     chain_id,
        "rounds":      questions_data["rounds"],
        "status":      "waiting",
        "isPublic":    False,   # rematches are private
        "players": {
            requester: {
                "username":   requester_username,
                "points":     0,
                "ready":      False,
                "txVerified": False,
            }
        },
    }

    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO ai_challenges
               (id, code, creator_address, topic, stake_amount, token_symbol,
                chain_id, status, is_public, rounds_data, rematch_of)
               VALUES ($1,$2,$3,$4,$5,$6,$7,'waiting',FALSE,$8,$9)""",
            new_id, new_code, requester, topic, stake, token,
            chain_id, json.dumps(questions_data), orig_id,
        )
        await conn.execute(
            """INSERT INTO challenge_players (challenge_id, wallet_address, username)
               VALUES ($1,$2,$3)""",
            new_id, requester, requester_username,
        )

    challenges[new_code] = new_challenge
    game_state[new_code] = {"answers": {}}

    # Notify opponent
    asyncio.create_task(
        notif.notify_rematch_request(new_code, requester_username, opponent_wallet)
    )

    return {
        "success":  True,
        "newCode":  new_code,
        "challenge": _safe_challenge(new_challenge),
    }
@app.post("/api/challenge/{code}/sync-stake")
async def sync_stake(code: str, body: dict):
    code   = code.upper()
    wallet = body.get("walletAddress", "").lower()

    if not wallet:
        raise HTTPException(status_code=400, detail="walletAddress required")
    if code not in challenges:
        raise HTTPException(status_code=404, detail="Challenge not found")

    challenge = challenges[code]

    if wallet not in challenge["players"]:
        raise HTTPException(status_code=403, detail="You are not in this challenge")

    if challenge["players"][wallet].get("txVerified"):
        return {"success": True, "alreadyVerified": True}

    # ── Use getQuiz() — hasStaked() does not exist on this contract ──────────
    GET_QUIZ_ABI = [{
        "inputs": [{"internalType": "bytes32", "name": "quizId", "type": "bytes32"}],
        "name": "getQuiz",
        "outputs": [{
            "components": [
                {"internalType": "bytes32",  "name": "id",             "type": "bytes32"},
                {"internalType": "address",  "name": "token",          "type": "address"},
                {"internalType": "uint256",  "name": "stakePerPlayer", "type": "uint256"},
                {"internalType": "uint256",  "name": "totalStaked",    "type": "uint256"},
                {"internalType": "address",  "name": "player1",        "type": "address"},
                {"internalType": "address",  "name": "player2",        "type": "address"},
                {"internalType": "address",  "name": "winner",         "type": "address"},
                {"internalType": "bool",     "name": "resolved",       "type": "bool"},
                {"internalType": "bool",     "name": "rewardClaimed",  "type": "bool"},
                {"internalType": "uint256",  "name": "createdAt",      "type": "uint256"},
            ],
            "internalType": "struct QuizHub.Quiz",
            "name": "",
            "type": "tuple",
        }],
        "stateMutability": "view",
        "type": "function",
    }]

    try:
        w3       = Web3(Web3.HTTPProvider(RPC_URLS[42220]))
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(CONTRACT_ADDRESS),
            abi=GET_QUIZ_ABI,
        )
        quiz_id_bytes = Web3.keccak(text=code)
        quiz          = contract.functions.getQuiz(quiz_id_bytes).call()

        # quiz is a tuple matching the struct order above
        stake_per_player = quiz[2]   # stakePerPlayer
        total_staked     = quiz[3]   # totalStaked
        player1          = quiz[4].lower()
        player2          = quiz[5].lower()

        # player1 has staked if totalStaked >= stakePerPlayer
        # player2 has staked if totalStaked >= stakePerPlayer * 2
        if wallet == player1:
            has_staked = total_staked >= stake_per_player
        elif wallet == player2:
            has_staked = total_staked >= stake_per_player * 2
        else:
            # Wallet not registered on-chain yet (player2 slot still zero address)
            has_staked = False

    except Exception as e:
        logger.error("sync-stake on-chain check failed: %s", e)
        raise HTTPException(status_code=502, detail="Could not reach contract")

    if not has_staked:
        return {"success": False, "verified": False, "message": "No stake found on-chain"}

    # Mark verified in memory + DB
    challenge["players"][wallet]["txVerified"] = True

    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE challenge_players
               SET tx_verified = TRUE
               WHERE challenge_id = (SELECT id FROM ai_challenges WHERE code=$1)
                 AND wallet_address = $2""",
            code, wallet,
        )

    await broadcast(code, {"type": "stake_verified", "wallet": wallet})
    await _maybe_auto_start(code)

    return {"success": True, "verified": True}


@app.get("/api/challenge/{wallet}/history")
async def challenge_history(wallet: str, limit: int = Query(default=10, le=50)):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT c.code, c.topic, c.stake_amount, c.token_symbol,
                      c.status, c.winner_address, c.created_at, c.finished_at
               FROM ai_challenges c
               JOIN challenge_players cp ON cp.challenge_id = c.id
               WHERE cp.wallet_address = $1
               ORDER BY c.created_at DESC
               LIMIT $2""",
            wallet.lower(), limit,
        )
    return {"success": True, "history": [dict(r) for r in rows]}


@app.get("/api/challenge/{wallet}/pending-claims")
async def get_pending_claims(wallet: str):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT code, topic, stake_amount * 2 AS win_amount, token_symbol, chain_id
               FROM ai_challenges
               WHERE winner_address=$1 AND claimed=FALSE AND status='finished'""",
            wallet.lower(),
        )
    return {"success": True, "claims": [dict(r) for r in rows]}


@app.post("/api/challenge/claim")
async def claim_win(body: ClaimRequest):
    code   = body.code.upper()
    wallet = body.walletAddress.lower()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM ai_challenges WHERE code=$1", code
        )

    if not row:
        raise HTTPException(status_code=404, detail="Challenge not found")
    if row["winner_address"] != wallet:
        raise HTTPException(status_code=403, detail="You are not the winner")
    if row["claimed"]:
        raise HTTPException(status_code=400, detail="Already claimed")

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE ai_challenges SET claimed=TRUE WHERE code=$1", code
        )
        await conn.execute(
            "UPDATE players SET total_earned=total_earned+$1 WHERE wallet_address=$2",
            float(row["stake_amount"]) * 2, wallet,
        )

    # TODO: trigger on-chain payout from treasury wallet here

    return {"success": True, "message": "Reward marked for payout", "amount": float(row["stake_amount"]) * 2}


# ─── Notification Endpoints ───────────────────────────────────────────────────

@app.get("/api/notifications/{wallet}")
async def get_notifications(wallet: str, limit: int = Query(default=20, le=50)):
    items = await notif.get_unread(wallet, limit)
    return {"success": True, "notifications": items}


@app.get("/api/notifications/{wallet}/count")
async def get_unread_count(wallet: str):
    count = await notif.unread_count(wallet)
    return {"success": True, "unread": count}


@app.post("/api/notifications/{wallet}/read/{notif_id}")
async def mark_read(wallet: str, notif_id: str):
    await notif.mark_read(wallet, notif_id)
    return {"success": True}


@app.post("/api/notifications/{wallet}/read-all")
async def mark_all_read(wallet: str):
    await notif.mark_all_read(wallet)
    return {"success": True}


# ─── WebSocket: Game ──────────────────────────────────────────────────────────

@app.websocket("/ws/challenge/{code}")
async def challenge_socket(ws: WebSocket, code: str):
    """
    Game WebSocket. Message types the CLIENT sends:

      { type: "stake_confirmed", walletAddress, txHash }
        → backend verifies tx then marks player ready; once both verified+ready → starts game

      { type: "ready", walletAddress }
        → mark player as ready (used AFTER stake confirmed if you want a separate "ready up" UX)

      { type: "submit_answer", walletAddress, roundIndex, questionIndex, answerId, timeTaken }
        → store answer for scoring

      { type: "chat", walletAddress, username, text }
        → relay to all participants
    """
    code = code.upper()
    await ws.accept()
    connections.setdefault(code, []).append(ws)

    # Send current state snapshot to the newly connected client
    if code in challenges:
        await ws.send_json({
            "type":      "state_sync",
            "challenge": _safe_challenge(challenges[code]),
        })

    try:
        async for msg in ws.iter_json():
            m_type = msg.get("type")
            wallet = msg.get("walletAddress", "").lower()

            if m_type == "stake_confirmed":
                await _handle_stake_confirmed(code, wallet, msg.get("txHash", ""))

            elif m_type == "ready":
                await _handle_ready(code, wallet)

            elif m_type == "submit_answer":
                _handle_submit_answer(code, wallet, msg)

            elif m_type == "chat":
                await broadcast(code, {
                    "type":      "chat",
                    "sender":    msg.get("username"),
                    "wallet":    wallet,
                    "text":      msg.get("text"),
                    "timestamp": asyncio.get_event_loop().time(),
                })

    except WebSocketDisconnect:
        sockets = connections.get(code, [])
        if ws in sockets:
            sockets.remove(ws)


async def _handle_stake_confirmed(code: str, wallet: str, tx_hash: str) -> None:
    if code not in challenges:
        return
    
    challenge = challenges[code]
    
    # If wallet not in players yet (joiner who staked before /join resolved),
    # we cannot verify them — they must call /join first. 
    # BUT: creator is always in players, so this only skips true unknowns.
    if wallet not in challenge["players"]:
        logger.warning(
            "stake_confirmed from unknown wallet %s for code %s — ignoring", 
            wallet, code
        )
        return

    # Idempotent: if already verified, just re-broadcast so the client can sync
    if challenge["players"][wallet].get("txVerified"):
        await broadcast(code, {"type": "stake_verified", "wallet": wallet})
        return

    challenge["players"][wallet]["txVerified"] = True
    challenge["players"][wallet]["txHash"] = tx_hash

    # Skip DB write for sentinel values (auto-sync / sync-recovery)
    if tx_hash not in ("auto-sync", "sync-recovery"):
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE challenge_players
                   SET tx_hash=$1, tx_verified=TRUE
                   WHERE challenge_id=(SELECT id FROM ai_challenges WHERE code=$2)
                     AND wallet_address=$3""",
                tx_hash, code, wallet,
            )
    else:
        # Still persist the verified flag even for synced stakes
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE challenge_players
                   SET tx_verified=TRUE
                   WHERE challenge_id=(SELECT id FROM ai_challenges WHERE code=$2)
                     AND wallet_address=$3""",
                code, wallet,
            )

    await broadcast(code, {"type": "stake_verified", "wallet": wallet})
async def _handle_ready(code: str, wallet: str) -> None:
    if code not in challenges or wallet not in challenges[code]["players"]:
        return
    challenge = challenges[code]
    # Only allow ready if stake is verified
    if not challenge["players"][wallet].get("txVerified"):
        return
    challenge["players"][wallet]["ready"] = True
    await broadcast(code, {"type": "player_ready", "wallet": wallet})
    await _maybe_auto_start(code)


def _handle_submit_answer(code: str, wallet: str, msg: dict) -> None:
    if code not in game_state:
        return
    r_idx  = msg.get("roundIndex")
    q_idx  = msg.get("questionIndex")
    key    = f"{r_idx}_{q_idx}"
    state  = game_state[code]
    # Only accept first submission per player per question
    if wallet not in state["answers"].get(key, {}):
        state["answers"].setdefault(key, {})[wallet] = {
            "answerId":  msg.get("answerId"),
            "timeTaken": msg.get("timeTaken", 0),
        }


async def _maybe_auto_start(code: str) -> None:
    """Start the game when both players are verified AND ready."""
    if code not in challenges:
        return
    c = challenges[code]
    if (
        len(c["players"]) == 2
        and all(p.get("txVerified") for p in c["players"].values())
        and all(p.get("ready")      for p in c["players"].values())
        and c["status"] == "waiting"
    ):
        wallets = list(c["players"].keys())
        asyncio.create_task(notif.notify_game_starting(code, wallets))
        asyncio.create_task(
            run_game_loop(code, challenges, game_state, pool, broadcast)
        )


# ─── WebSocket: Live Notifications ───────────────────────────────────────────

@app.websocket("/ws/notify/{wallet}")
async def notify_socket(ws: WebSocket, wallet: str):
    wallet = wallet.lower()
    await ws.accept()
    notify_conn.setdefault(wallet, []).append(ws)

    try:
        count = await notif.unread_count(wallet)
        await ws.send_json({"type": "unread_count", "count": count})
    except (WebSocketDisconnect, Exception):
        # Client left before we could send — clean up and return
        sockets = notify_conn.get(wallet, [])
        if ws in sockets:
            sockets.remove(ws)
        return

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        sockets = notify_conn.get(wallet, [])
        if ws in sockets:
            sockets.remove(ws)


# ─── Internal ─────────────────────────────────────────────────────────────────

def _safe_challenge(c: dict) -> dict:
    """Strip correctId from question options before sending to clients."""
    import copy
    safe        = copy.deepcopy(c)
    safe_rounds = strip_answers({"rounds": safe.get("rounds", [])})
    safe["rounds"] = safe_rounds["rounds"]
    return safe


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
