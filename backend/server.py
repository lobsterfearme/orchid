from fastapi import FastAPI, APIRouter
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
import hashlib
import hmac
import time
import json
import requests
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional
import uuid
from datetime import datetime, timezone

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection (optional - only needed for status endpoints)
mongo_url = os.environ.get('MONGO_URL', '')
db_name = os.environ.get('DB_NAME', 'orchid')
if mongo_url:
    mongo_client = AsyncIOMotorClient(mongo_url)
    db = mongo_client[db_name]
else:
    mongo_client = None
    db = None

# Tuya API config - tuya app project (Access ID starts with 3)
TUYA_CLIENT_ID = os.environ.get('TUYA_CLIENT_ID', '3vpcksvjswdgrujus57c')
TUYA_CLIENT_SECRET = os.environ.get('TUYA_CLIENT_SECRET', '5a4409e16e9f454998853507a189d42a')
TUYA_BASE_URL = 'https://openapi.tuyaus.com'

# Weather API config (OpenWeatherMap)
WEATHER_API_KEY = os.environ.get('WEATHER_API_KEY', '')
WEATHER_LAT = os.environ.get('WEATHER_LAT', '25.7617')
WEATHER_LON = os.environ.get('WEATHER_LON', '-80.1918')

# Misting threshold - feels like temp in Fahrenheit
FEELS_LIKE_THRESHOLD_F = 90.0

# Create the main app without a prefix
app = FastAPI()

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# Tuya helpers

def _tuya_sign(client_id: str, secret: str, access_token: str, t: str, nonce: str, string_to_sign: str) -> str:
    message = client_id + access_token + t + nonce + string_to_sign
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest().upper()


def tuya_get_token() -> str:
    t = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())
    string_to_sign = 'GET\n' + hashlib.sha256(b'').hexdigest() + '\n\n' + '/v1.0/token?grant_type=1'
    sign = _tuya_sign(TUYA_CLIENT_ID, TUYA_CLIENT_SECRET, '', t, nonce, string_to_sign)
    headers = {
        'client_id': TUYA_CLIENT_ID,
        'sign': sign,
        't': t,
        'nonce': nonce,
        'sign_method': 'HMAC-SHA256',
    }
    resp = requests.get(f'{TUYA_BASE_URL}/v1.0/token?grant_type=1', headers=headers, timeout=10)
    data = resp.json()
    if not data.get('success'):
        raise Exception(f"Tuya token error: {data}")
    return data['result']['access_token']


def tuya_request(method: str, path: str, body: dict = None) -> dict:
    access_token = tuya_get_token()
    t = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())
    body_str = json.dumps(body) if body else ''
    content_hash = hashlib.sha256(body_str.encode()).hexdigest()
    string_to_sign = method.upper() + '\n' + content_hash + '\n\n' + path
    sign = _tuya_sign(TUYA_CLIENT_ID, TUYA_CLIENT_SECRET, access_token, t, nonce, string_to_sign)
    headers = {
        'client_id': TUYA_CLIENT_ID,
        'access_token': access_token,
        'sign': sign,
        't': t,
        'nonce': nonce,
        'sign_method': 'HMAC-SHA256',
        'Content-Type': 'application/json',
    }
    url = TUYA_BASE_URL + path
    if method.upper() == 'GET':
        resp = requests.get(url, headers=headers, timeout=10)
    else:
        resp = requests.post(url, headers=headers, data=body_str, timeout=10)
    return resp.json()


def control_device(device_id: str, commands: list) -> dict:
    path = f'/v1.0/devices/{device_id}/commands'
    return tuya_request('POST', path, {'commands': commands})


# Weather helpers

def kelvin_to_fahrenheit(k: float) -> float:
    return (k - 273.15) * 9 / 5 + 32


def get_weather() -> dict:
    """Fetch current weather including feels_like from OpenWeatherMap."""
    if not WEATHER_API_KEY:
        return {'error': 'No weather API key configured'}
    url = (
        f'https://api.openweathermap.org/data/2.5/weather'
        f'?lat={WEATHER_LAT}&lon={WEATHER_LON}&appid={WEATHER_API_KEY}'
    )
    resp = requests.get(url, timeout=10)
    data = resp.json()
    if resp.status_code != 200:
        return {'error': data}
    feels_like_k = data['main']['feels_like']
    temp_k = data['main']['temp']
    feels_like_f = kelvin_to_fahrenheit(feels_like_k)
    temp_f = kelvin_to_fahrenheit(temp_k)
    return {
        'temp_f': round(temp_f, 1),
        'feels_like_f': round(feels_like_f, 1),
        'humidity': data['main']['humidity'],
        'wind_speed_mph': round(data['wind']['speed'] * 2.237, 1),
        'description': data['weather'][0]['description'],
        'high_heat_alert': feels_like_f >= FEELS_LIKE_THRESHOLD_F,
    }


# Models

class StatusCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_name: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class StatusCheckCreate(BaseModel):
    client_name: str


class DeviceCommand(BaseModel):
    device_id: str
    commands: list


class MistRequest(BaseModel):
    device_id: str
    duration_seconds: int = 30


# Routes

@api_router.get("/")
async def root():
    return {"message": "Orchid Care API", "tuya_client_id": TUYA_CLIENT_ID[:8] + "..."}


@api_router.post("/status", response_model=StatusCheck)
async def create_status_check(input: StatusCheckCreate):
    if db is None:
        return StatusCheck(client_name=input.client_name)
    status_dict = input.model_dump()
    status_obj = StatusCheck(**status_dict)
    doc = status_obj.model_dump()
    doc['timestamp'] = doc['timestamp'].isoformat()
    await db.status_checks.insert_one(doc)
    return status_obj


@api_router.get("/status", response_model=List[StatusCheck])
async def get_status_checks():
    if db is None:
        return []
    status_checks = await db.status_checks.find({}, {"_id": 0}).to_list(1000)
    for check in status_checks:
        if isinstance(check['timestamp'], str):
            check['timestamp'] = datetime.fromisoformat(check['timestamp'])
    return status_checks


@api_router.get("/weather")
async def weather():
    """Get current weather + feels-like temperature."""
    return get_weather()


@api_router.get("/devices")
async def list_devices():
    """List all Tuya devices linked to the project."""
    try:
        result = tuya_request('GET', '/v1.0/devices?page_size=20')
        return result
    except Exception as e:
        return {"error": str(e)}


@api_router.post("/devices/command")
async def send_command(cmd: DeviceCommand):
    """Send a command to a Tuya device."""
    try:
        result = control_device(cmd.device_id, cmd.commands)
        return result
    except Exception as e:
        return {"error": str(e)}


@api_router.post("/mist")
async def trigger_mist(req: MistRequest):
    """
    Trigger a mist cycle on demand.
    Turns valve ON for the specified duration.
    """
    try:
        on_result = control_device(req.device_id, [{"code": "switch", "value": True}])
        logger.info(f"Mist ON for device {req.device_id}: {on_result}")
        return {
            "status": "mist_started",
            "device_id": req.device_id,
            "duration_seconds": req.duration_seconds,
            "on_result": on_result,
        }
    except Exception as e:
        return {"error": str(e)}


@api_router.post("/mist/stop")
async def stop_mist(req: MistRequest):
    """Turn misting valve off."""
    try:
        result = control_device(req.device_id, [{"code": "switch", "value": False}])
        return {"status": "mist_stopped", "device_id": req.device_id, "result": result}
    except Exception as e:
        return {"error": str(e)}


@api_router.get("/mist/check")
async def check_mist_needed():
    """
    Check if an extra mist cycle is needed based on feels-like temperature.
    - Feels like >= 90F: add extra mist cycle + water orchids more frequently
    - Feels like < 90F: normal 2-cycle schedule
    """
    weather_data = get_weather()
    if 'error' in weather_data:
        return weather_data

    feels_like = weather_data['feels_like_f']
    extra_mist = feels_like >= FEELS_LIKE_THRESHOLD_F

    if feels_like >= FEELS_LIKE_THRESHOLD_F:
        mist_cycles = 3  # extra cycle added at 90F+ feels-like
        watering_note = (
            f"Feels like {feels_like}F — HIGH HEAT: adding extra mist cycle "
            f"and increased orchid watering frequency."
        )
    else:
        mist_cycles = 2  # normal schedule
        watering_note = f"Feels like {feels_like}F — normal misting schedule."

    return {
        **weather_data,
        "extra_mist_cycle": extra_mist,
        "recommended_mist_cycles": mist_cycles,
        "watering_note": watering_note,
        "threshold_f": FEELS_LIKE_THRESHOLD_F,
    }


# App setup

app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("shutdown")
async def shutdown_db_client():
    if mongo_client:
        mongo_client.close()
