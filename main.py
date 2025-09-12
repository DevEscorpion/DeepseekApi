#!/usr/bin/env python3
"""
Servidor API compatible con OpenAI para DeepSeek con soporte para DeepThink.
OPTIMIZADO PARA BAJA LATENCIA Y ALTA CONCURRENCIA.

- I/O totalmente asíncrono para no bloquear el servidor.
- Cacheo inteligente del Proof-of-Work para eliminar latencia de red y CPU.
- Pre-carga del módulo WASM en el arranque.
- Sistema avanzado de gestión de sesiones con pool interno automático.

Levanta un servidor local en http://localhost:5001 que traduce las peticiones
de la API de OpenAI al protocolo no oficial de DeepSeek.

Modelos disponibles:
- deepseek-chat: Modelo estándar sin razonamiento
- deepseek-reasoner: Modelo con capacidad de razonamiento (DeepThink)

Requisitos:
- pip install fastapi uvicorn "curl_cffi[brotli]" wasmtime orjson
- Tener el archivo 'sha3_wasm_bg.7b9ca65ddd.wasm' en la misma carpeta.

Ejecución:
1. Edita las variables DEEPSEEK_EMAIL y DEEPSEEK_PASSWORD en este archivo.
2. Guarda el archivo como 'main_proxy.py'.
3. Ejecuta desde la terminal: uvicorn main_proxy:app --host 0.0.0.0 --port 5001

Configuración en clientes (Genie, CodeGPT, etc.):
- API Endpoint / Base URL: http://localhost:5001/v1
- API Key: Cualquier cosa (ej: "sk-12345")
- Models: deepseek-chat (normal) o deepseek-reasoner (con razonamiento)
"""
import base64
import ctypes
import json
import logging
import struct
import time
import uuid
import hashlib
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple
from collections import deque

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from curl_cffi import AsyncSession
from wasmtime import Linker, Module, Store, Instance

# --- CONFIGURACIÓN DE CREDENCIALES ---
DEEPSEEK_EMAIL = os.environ.get("DEEPSEEK_EMAIL", "")
DEEPSEEK_PASSWORD = os.environ.get("DEEPSEEK_PASSWORD", "")
# -------------------------------------


# --- INICIALIZACIÓN DE LA APP Y LOGGING ---
app = FastAPI()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("DeepSeekProxy")

# --- CONSTANTES DE LA API DEEPSEEK ---
DEEPSEEK_HOST = "chat.deepseek.com"
DEEPSEEK_LOGIN_URL = f"https://{DEEPSEEK_HOST}/api/v0/users/login"
DEEPSEEK_CREATE_SESSION_URL = f"https://{DEEPSEEK_HOST}/api/v0/chat_session/create"
DEEPSEEK_CREATE_POW_URL = f"https://{DEEPSEEK_HOST}/api/v0/chat/create_pow_challenge"
DEEPSEEK_COMPLETION_URL = f"https://{DEEPSEEK_HOST}/api/v0/chat/completion"
WASM_PATH = "sha3_wasm_bg.7b9ca65ddd.wasm"

# Cabeceras base mejoradas para parecer más un navegador real
BASE_HEADERS = {
    "Host": DEEPSEEK_HOST,
    "Connection": "keep-alive",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
    "sec-ch-ua": '"Chromium";v="125", "Not.A/Brand";v="8"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
    "Priority": "u=0, i",
    "x-client-platform": "web",
    "x-client-version": "1.3.0-auto-resume",
    "x-client-locale": "en_US",
    "accept-charset": "UTF-8",
    "x-app-version": "20241129.1",
}

# --- OPTIMIZACIÓN: GLOBALES PARA REUTILIZACIÓN ---
# Sesión HTTP asíncrona para reutilizar conexiones
async_http_session: Optional[AsyncSession] = None

# Variables para el módulo WASM pre-cargado
WASM_STORE: Optional[Store] = None
WASM_INSTANCE: Optional[Instance] = None

# <-- CAMBIO: Se elimina el cacheo de PoW para forzar su regeneración en cada petición -->
# POW_CACHE: Dict[str, Any] = {"response": None, "expiry": datetime.min.replace(tzinfo=timezone.utc)}
# POW_CACHE_LOCK = asyncio.Lock()
# --------------------------------------------------

# --- Rate Limiting: Token Bucket simple ---
# Configuración (ajusta estos valores)
MAX_TOKENS = 3  # Número máximo de tokens (ráfaga permitida)
TOKENS_PER_MINUTE = 3  # Tokens agregados por minuto (tasa sostenida)

# Estado del bucket (protegido por lock para concurrencia)
rate_limit_tokens = MAX_TOKENS
rate_limit_lock = asyncio.Lock()
last_token_add_time = time.time()
# --- FIN Rate Limiting ---

@app.on_event("startup")
async def startup_event():
    """Eventos de inicio: inicializar sesión HTTP y cargar WASM."""
    global async_http_session, WASM_STORE, WASM_INSTANCE
    
    # OPTIMIZACIÓN: Crear una única sesión HTTP para reutilizar conexiones
    async_http_session = AsyncSession(impersonate="chrome110", timeout=30)
    logger.info("🚀 Sesión HTTP asíncrona inicializada.")

    # OPTIMIZACIÓN: Pre-cargar y compilar el módulo WASM una sola vez
    try:
        logger.info("⏳ Cargando y compilando módulo WASM...")
        WASM_STORE = Store()
        module = Module.from_file(WASM_STORE.engine, WASM_PATH)
        linker = Linker(WASM_STORE.engine)
        WASM_INSTANCE = linker.instantiate(WASM_STORE, module)
        logger.info("✅ Módulo WASM cargado en memoria.")
    except Exception as e:
        logger.critical(f"❌ No se pudo cargar el archivo WASM en '{WASM_PATH}': {e}")
        # La aplicación puede continuar, pero get_pow_response fallará.
        # Se podría optar por salir si el WASM es crítico: exit(1)

@app.on_event("shutdown")
async def shutdown_event():
    """Cerrar la sesión HTTP al apagar."""
    if async_http_session:
        await async_http_session.close()
        logger.info("🌙 Sesión HTTP cerrada.")

# --- Función para rate limiting ---
async def _acquire_token():
    """Adquiere un token del bucket, esperando si es necesario."""
    global rate_limit_tokens, last_token_add_time
    async with rate_limit_lock:
        current_time = time.time()
        # Agregar tokens basados en el tiempo transcurrido
        time_passed = current_time - last_token_add_time
        tokens_to_add = (time_passed / 60.0) * TOKENS_PER_MINUTE
        if tokens_to_add > 0:
            rate_limit_tokens = min(MAX_TOKENS, rate_limit_tokens + tokens_to_add)
            last_token_add_time = current_time

        if rate_limit_tokens >= 1:
            rate_limit_tokens -= 1
            return # Tenemos token, proceder
        else:
            # No hay tokens, calcular tiempo de espera hasta el próximo
            wait_time = max(1.0 / TOKENS_PER_MINUTE * 60, 0.1) # Min 0.1s
            logger.info(f"⏳ Rate limit alcanzado. Esperando {wait_time:.2f}s antes de la próxima solicitud.")
            await asyncio.sleep(wait_time)
            # Recursivamente intentar adquirir un token después de esperar
            await _acquire_token()
# --- Fin función para rate limiting ---

# --- SISTEMA AVANZADO DE GESTIÓN DE SESIONES CON POOL ---
class AdvancedSessionManager:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._token: Optional[str] = None
        self._token_expiry: Optional[datetime] = None
        self._session_pool: Dict[str, Dict[str, Any]] = {}
        
        self._client_sessions: Dict[str, Dict[str, str]] = {}
        
        self._available_sessions: Dict[str, deque] = {
            "deepseek-chat": deque(),
            "deepseek-reasoner": deque()
        }
        self._stats = {"sessions_created": 0, "sessions_reused": 0, "clients_served": 0, "pool_hits": 0, "pool_misses": 0}

    def _generate_unique_client_id(self, request_info: dict) -> str:
        unique_string = f"{time.time()}_{uuid.uuid4().hex[:8]}_{request_info.get('user_agent', 'unknown')}_{request_info.get('model', 'default')}"
        return f"client_{hashlib.md5(unique_string.encode()).hexdigest()[:12]}"

    def _detect_returning_client(self, request_info: dict) -> Optional[str]:
        headers = request_info.get("headers", {})
        if "x-client-session" in headers:
            return headers["x-client-session"]
        if "authorization" in headers:
            auth_hash = hashlib.md5(headers["authorization"].encode()).hexdigest()[:8]
            return f"auth_{auth_hash}"
        return None

    async def get_token(self) -> Optional[str]:
        async with self._lock:
            if self._token and self._token_expiry and datetime.now() < self._token_expiry:
                return self._token
            return None

    async def set_token(self, token: str, hours_valid: int = 24):
        async with self._lock:
            self._token = token
            self._token_expiry = datetime.now() + timedelta(hours=hours_valid)

    async def invalidate_token(self):
        async with self._lock:
            self._token = None
            self._token_expiry = None

    def _create_session_info(self, session_id: str, model: str, client_id: str) -> dict:
        return {"id": session_id, "model": model, "client_id": client_id, "created": datetime.now(), "last_used": datetime.now(), "expiry": datetime.now() + timedelta(hours=2), "usage_count": 0, "status": "active"}

    def _find_available_session(self, model: str) -> Optional[str]:
        available = self._available_sessions.get(model, deque())
        while available:
            session_id = available.popleft()
            if session_id in self._session_pool:
                session_info = self._session_pool[session_id]
                if datetime.now() < session_info["expiry"]:
                    self._stats["pool_hits"] += 1
                    logger.info(f"♻️ Sesión reutilizada del pool: {session_id[:8]}... (modelo: {model})")
                    return session_id
                else:
                    del self._session_pool[session_id]
        self._stats["pool_misses"] += 1
        return None

    async def get_or_create_session(self, request_info: dict) -> Tuple[str, Optional[str]]:
        async with self._lock:
            model = request_info.get("model", "deepseek-chat")
            client_id = self._detect_returning_client(request_info)
            if not client_id:
                client_id = self._generate_unique_client_id(request_info)
                self._stats["clients_served"] += 1
                logger.info(f"🆕 Nuevo cliente generado: {client_id}")
            
            client_model_sessions = self._client_sessions.get(client_id, {})
            existing_session_id = client_model_sessions.get(model)

            if existing_session_id:
                if (existing_session_id in self._session_pool and datetime.now() < self._session_pool[existing_session_id]["expiry"]):
                    session_info = self._session_pool[existing_session_id]
                    session_info["last_used"] = datetime.now()
                    session_info["usage_count"] += 1
                    self._stats["sessions_reused"] += 1
                    logger.info(f"🔄 Reutilizando sesión existente: {existing_session_id[:8]}... (cliente: {client_id}, modelo: {model})")
                    return client_id, existing_session_id
                else:
                    if model in client_model_sessions:
                        del client_model_sessions[model]
                    if existing_session_id in self._session_pool:
                        del self._session_pool[existing_session_id]
            
            session_id = self._find_available_session(model)
            if session_id is None:
                logger.info(f"🏗️ Necesaria nueva sesión para cliente {client_id} (modelo: {model})")
                return client_id, None

            self._client_sessions.setdefault(client_id, {})[model] = session_id
            session_info = self._session_pool[session_id]
            session_info["client_id"] = client_id
            session_info["last_used"] = datetime.now()
            session_info["usage_count"] += 1
            logger.info(f"✅ Sesión del pool asignada: {session_id[:8]}... → {client_id} (modelo: {model})")
            return client_id, session_id

    async def register_new_session(self, client_id: str, session_id: str, model: str):
        async with self._lock:
            session_info = self._create_session_info(session_id, model, client_id)
            self._session_pool[session_id] = session_info
            self._client_sessions.setdefault(client_id, {})[model] = session_id
            self._stats["sessions_created"] += 1
            logger.info(f"📝 Nueva sesión registrada: {session_id[:8]}... → {client_id} (modelo: {model})")

    async def cleanup_expired_sessions(self):
        async with self._lock:
            now = datetime.now()
            expired_ids = {sid for sid, info in self._session_pool.items() if now >= info["expiry"]}
            if not expired_ids: return

            self._session_pool = {sid: info for sid, info in self._session_pool.items() if sid not in expired_ids}
            
            clients_to_clean = []
            for cid, model_sessions in self._client_sessions.items():
                sessions_to_remove = {model for model, sid in model_sessions.items() if sid in expired_ids}
                for model in sessions_to_remove:
                    del model_sessions[model]
                if not model_sessions:
                    clients_to_clean.append(cid)
            
            for cid in clients_to_clean:
                del self._client_sessions[cid]

            for model_queue in self._available_sessions.values():
                new_queue = deque([sid for sid in model_queue if sid not in expired_ids])
                model_queue.clear()
                model_queue.extend(new_queue)
            
            if expired_ids:
                logger.info(f"🧹 Limpiadas {len(expired_ids)} sesiones expiradas.")
    
    async def get_detailed_stats(self) -> dict:
        async with self._lock:
            token_valid = self._token and self._token_expiry and datetime.now() < self._token_expiry
            token_hours_left = 0
            if token_valid:
                token_hours_left = round((self._token_expiry - datetime.now()).total_seconds() / 3600, 1)

            active_client_sessions = sum(len(sessions) for sessions in self._client_sessions.values())

            return {
                "token_valid": token_valid,
                "token_hours_left": token_hours_left,
                "pool_stats": {
                    "total_sessions": len(self._session_pool),
                    "available_sessions": {k: len(v) for k, v in self._available_sessions.items()},
                    "active_clients": len(self._client_sessions),
                    "active_client_sessions": active_client_sessions
                },
                "usage_stats": self._stats
            }
session_manager = AdvancedSessionManager()

# --- LÓGICA DE COMUNICACIÓN ASÍNCRONA CON DEEPSEEK ---

async def login_deepseek(force_new: bool = False) -> Optional[str]:
    if not force_new:
        cached_token = await session_manager.get_token()
        if cached_token: return cached_token
    
    logger.info("🔑 Realizando nuevo login en DeepSeek...")
    payload = {"email": DEEPSEEK_EMAIL, "password": DEEPSEEK_PASSWORD, "device_id": "deepseek_to_api", "os": "android"}
    try:
        resp = await async_http_session.post(DEEPSEEK_LOGIN_URL, headers=BASE_HEADERS, json=payload)
        resp.raise_for_status()
        data = resp.json()
        
        token = data.get("data", {}).get("biz_data", {}).get("user", {}).get("token")
        
        if not token: 
            logger.warning(f"Respuesta de login no contiene token. Respuesta recibida: {data}")
            raise ValueError("No se encontró token en la respuesta de login.")
        
        await session_manager.set_token(token)
        logger.info("✅ Login exitoso, nuevo token obtenido.")
        return token
    except Exception as e:
        logger.error(f"❌ Fallo en el login: {e}")
        return None

async def create_deepseek_session(token: str, model: str) -> Optional[str]:
    logger.info(f"🏗️ Creando nueva sesión en DeepSeek (modelo: {model})...")
    headers = {**BASE_HEADERS, "authorization": f"Bearer {token}"}
    try:
        resp = await async_http_session.post(DEEPSEEK_CREATE_SESSION_URL, headers=headers, json={"agent": "chat"})
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == 0:
            session_id = data["data"]["biz_data"]["id"]
            logger.info(f"✅ Nueva sesión creada: {session_id[:8]}...")
            return session_id
        raise ValueError(f"Error al crear sesión: {data.get('msg')}")
    except Exception as e:
        logger.error(f"❌ Fallo al crear sesión: {e}")
        return None

def _compute_pow_answer(challenge_str: str, salt: str, expire_at: int, difficulty: int) -> int:
    """Función síncrona para el cálculo de PoW. Usa el WASM pre-cargado."""
    if not WASM_INSTANCE or not WASM_STORE:
        raise RuntimeError("El módulo WASM no está inicializado.")
    
    prefix = f"{salt}_{expire_at}_"
    exports = WASM_INSTANCE.exports(WASM_STORE)
    memory = exports["memory"]; alloc = exports["__wbindgen_export_0"]; wasm_solve = exports["wasm_solve"]; add_to_stack = exports["__wbindgen_add_to_stack_pointer"]
    
    def write_memory(offset, data): ctypes.memmove(ctypes.cast(memory.data_ptr(WASM_STORE), ctypes.c_void_p).value + offset, data, len(data))
    def read_memory(offset, size): return ctypes.string_at(ctypes.cast(memory.data_ptr(WASM_STORE), ctypes.c_void_p).value + offset, size)
    def encode_string(text):
        data = text.encode("utf-8")
        ptr = int(alloc(WASM_STORE, len(data), 1))
        write_memory(ptr, data)
        return ptr, len(data)

    retptr = add_to_stack(WASM_STORE, -16)
    ptr_challenge, len_challenge = encode_string(challenge_str)
    ptr_prefix, len_prefix = encode_string(prefix)
    
    wasm_solve(WASM_STORE, retptr, ptr_challenge, len_challenge, ptr_prefix, len_prefix, float(difficulty))
    
    status = struct.unpack("<i", read_memory(retptr, 4))[0]
    value = struct.unpack("<d", read_memory(retptr + 8, 8))[0]
    add_to_stack(WASM_STORE, 16)
    
    if status == 0: raise ValueError("Cálculo de PoW falló en WASM.")
    return int(value)

# <-- CAMBIO: La función ahora genera un PoW nuevo en cada llamada, sin usar caché -->
async def get_pow_response(token: str) -> Optional[str]:
    """
    Obtiene y resuelve un nuevo Proof-of-Work para cada petición.
    """
    logger.info("⏳ Generando nuevo Proof-of-Work para la petición...")
    headers = {**BASE_HEADERS, "authorization": f"Bearer {token}"}
    try:
        # 1. Obtener el reto (I/O)
        resp = await async_http_session.post(DEEPSEEK_CREATE_POW_URL, headers=headers, json={"target_path": "/api/v0/chat/completion"})
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise ValueError(f"Error al obtener PoW: {data.get('msg')}")
        
        challenge = data["data"]["biz_data"]["challenge"]
        start_time = time.perf_counter()

        # 2. Computar la respuesta (CPU)
        answer = _compute_pow_answer(challenge["challenge"], challenge["salt"], challenge["expire_at"], challenge["difficulty"])
        
        solve_time = time.perf_counter() - start_time
        logger.info(f"✅ PoW resuelto en {solve_time:.3f}s.")
        
        pow_dict = {
            "algorithm": challenge["algorithm"],
            "challenge": challenge["challenge"],
            "salt": challenge["salt"],
            "answer": answer,
            "signature": challenge["signature"],
            "target_path": "/api/v0/chat/completion",
        }
        pow_response = base64.b64encode(json.dumps(pow_dict, separators=(",", ":")).encode()).decode()
        
        return pow_response
    except Exception as e:
        logger.error(f"❌ Fallo al obtener PoW: {e}", exc_info=True)
        return None
        
# --- ENDPOINTS DE LA API ---

@app.get("/")
async def root():
    stats = await session_manager.get_detailed_stats()
    return JSONResponse(content={
        "service": "DeepSeek Proxy Server - Latency Optimized",
        "version": "4.0.1-no-pow-cache", "status": "running",
        "features": ["Async I/O", "WASM Pre-loading", "Session Pooling", "Rate Limiting"],
        "endpoints": {"models": "/v1/models", "chat": "/v1/chat/completions", "stats": "/stats"},
        "session_stats": stats
    })

@app.get("/stats")
async def get_stats():
    return JSONResponse(content=await session_manager.get_detailed_stats())

@app.get("/v1/models")
def list_models():
    return JSONResponse(content={"object": "list", "data": [{"id": "deepseek-chat", "object": "model", "owned_by": "deepseek"}, {"id": "deepseek-reasoner", "object": "model", "owned_by": "deepseek"}]})

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    retry_count = 0
    max_retries = 2
    
    while retry_count <= max_retries:
        try:
            # Limpieza periódica. Se podría mover a una tarea en segundo plano.
            if time.time() % 300 < 1: # Aproximadamente cada 5 minutos
                 asyncio.create_task(session_manager.cleanup_expired_sessions())

            req_data = await request.json()
            model = req_data.get("model", "deepseek-chat")
            request_info = {"headers": dict(request.headers), "model": model, "user_agent": request.headers.get("user-agent", "unknown")}
            
            token = await login_deepseek(force_new=(retry_count > 0))
            if not token: raise HTTPException(status_code=500, detail="No se pudo obtener token de DeepSeek.")
            
            client_id, session_id = await session_manager.get_or_create_session(request_info)
            
            if session_id is None:
                session_id = await create_deepseek_session(token, model)
                if not session_id:
                    if retry_count < max_retries:
                        retry_count += 1; logger.warning("⚠️ Fallo al crear sesión, reintentando..."); await session_manager.invalidate_token(); continue
                    raise HTTPException(status_code=500, detail="No se pudo crear sesión en DeepSeek.")
                await session_manager.register_new_session(client_id, session_id, model)

            pow_response = await get_pow_response(token)
            if not pow_response: raise HTTPException(status_code=500, detail="No se pudo resolver el Proof-of-Work.")

            logger.info(f"💬 Petición: Cliente={client_id}, Modelo={model}, Sesión={session_id[:8]}..., Thinking={'Sí' if model == 'deepseek-reasoner' else 'No'}")
            
            prompt = "\n".join([msg.get('content', '') for msg in req_data.get("messages", [])])
            use_thinking = (model == "deepseek-reasoner")
            
            # --- AÑADIR Rate Limiting ---
            # Aplicar rate limiting antes de la solicitud principal
            await _acquire_token()
            # --- FIN AÑADIR Rate Limiting ---
            
            headers = {**BASE_HEADERS, "authorization": f"Bearer {token}", "x-ds-pow-response": pow_response}
            payload = {
                "chat_session_id": session_id,
                "parent_message_id": None,
                "prompt": prompt,
                "ref_file_ids": [],
                "thinking_enabled": use_thinking,
                "search_enabled": use_thinking,
            }
            
            deepseek_resp = await async_http_session.post(DEEPSEEK_COMPLETION_URL, headers=headers, json=payload, stream=True)
            
            if deepseek_resp.status_code == 401:
                logger.warning("⚠️ Token expirado (401), reintentando...")
                await session_manager.invalidate_token()
                retry_count += 1
                continue
            deepseek_resp.raise_for_status()

            async def stream_generator():
                completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
                created_time = int(time.time())
                has_yielded_content = False # Bandera para saber si se envió algo

                try:
                    async for line_bytes in deepseek_resp.aiter_lines():
                        line = line_bytes.decode('utf-8')

                        if line.startswith("data:"):
                            data_str = line[5:].strip()
                            if not data_str or data_str == "{}":
                                continue
                            try:
                                chunk = json.loads(data_str)
                                content_value = chunk.get("v")
                                path = chunk.get("p")

                                # Ajusta esta condición según los tipos de contenido que quieras enviar
                                # Incluye 'response/thinking_content' si 'use_thinking' es True
                                if isinstance(content_value, str) and (
                                    path == "response/content" or
                                    path is None or
                                    (use_thinking and path == "response/thinking_content")
                                ):
                                    openai_chunk = {
                                        "id": completion_id,
                                        "object": "chat.completion.chunk",
                                        "created": created_time,
                                        "model": model,
                                        "choices": [{
                                            "index": 0,
                                            "delta": {"content": content_value},
                                            "finish_reason": None
                                        }]
                                    }
                                    yield f"data: {json.dumps(openai_chunk)}\n\n"
                                    has_yielded_content = True

                            except (json.JSONDecodeError, TypeError) as e:
                                # Loggear errores de parseo pero continuar
                                logger.warning(f"Error parsing chunk: {e}, data_str: {data_str}")
                                continue

                    # --- Envío del chunk final y [DONE] ---
                    # Se envían siempre, incluso si no se envió contenido previo
                    if has_yielded_content: # Solo log si hubo contenido real
                        logger.info(f"✅ Respuesta completada para sesión {session_id[:8]}...")

                    # Enviar chunk final con finish_reason
                    final_chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created_time,
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop"
                        }]
                    }
                    yield f"data: {json.dumps(final_chunk)}\n\n"

                    # Enviar el mensaje de finalización del SSE
                    yield "data: [DONE]\n\n"

                except Exception as e:
                    logger.error(f"Error in stream_generator for session {session_id[:8]}: {e}", exc_info=True)
                    # Intentar enviar un mensaje de error al cliente si la conexión aún está abierta
                    error_chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created_time,
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {"content": f"\n[Proxy Error: {str(e)}]"},
                            "finish_reason": "error"
                        }]
                    }
                    yield f"data: {json.dumps(error_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                    # Re-lanzar para que FastAPI/Uvicorn maneje el cierre
                    raise

            if req_data.get("stream", False):
                return StreamingResponse(stream_generator(), media_type="text/event-stream")
            else:
                full_content = []
                async for line_bytes in deepseek_resp.aiter_lines():
                    line = line_bytes.decode('utf-8')
                    
                    if line.startswith("data:"):
                        data_str = line[5:].strip()
                        if not data_str or data_str == "{}": continue
                        try:
                            chunk = json.loads(data_str)
                            content_value = chunk.get("v")
                            if isinstance(content_value, str):
                                full_content.append(content_value)
                        except (json.JSONDecodeError, TypeError):
                            continue
                
                final_response = "".join(full_content)
                logger.info(f"✅ Respuesta no-streaming: {len(final_response)} caracteres")
                
                return JSONResponse(content={
                    "id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion",
                    "created": int(time.time()), "model": model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": final_response}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": len(prompt.split()), "completion_tokens": len(final_response.split()), "total_tokens": len(prompt.split()) + len(final_response.split())}
                })
            
            break
            
        except HTTPException as e:
            raise e
        except Exception as e:
            if retry_count < max_retries:
                retry_count += 1
                logger.warning(f"⚠️ Error en intento {retry_count+1}: {e}. Reintentando en 1 segundo...")
                await asyncio.sleep(1)
                continue
            logger.error(f"❌ Error fatal después de {max_retries+1} intentos.", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Error interno del servidor: {e}")
        
if __name__ == "__main__":
    import uvicorn
    print("\n" + "="*80)
    print("🚀 DeepSeek Proxy Server v4.0.1 - MODO: PoW POR PETICIÓN + RATE LIMITING")
    print("="*80)
    print("\n✅ OPTIMIZACIONES ACTIVAS:")
    print("  ⚡ I/O Totalmente Asíncrono (no bloqueante)")
    print
