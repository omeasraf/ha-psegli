#!/usr/bin/env python3
"""PSEG Long Island Automation Addon - FastAPI Server"""

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Dict, Optional

# Set HEADED=1 to run browser in headed mode (visible) for local MFA debugging
HEADED = os.environ.get("HEADED", "").lower() in ("1", "true", "yes")
import aiohttp
from fastapi import FastAPI, HTTPException, Form
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn

from auto_login import get_fresh_cookies, PSEGAutoLogin

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="PSEG Long Island Automation", version="1.0.0")

if HEADED:
    logger.info("HEADED mode enabled - browser window will be visible for MFA debugging")

# Store in-progress MFA session (single session at a time)
_mfa_session: Optional[PSEGAutoLogin] = None
_mfa_lock = asyncio.Lock()

class LoginRequest(BaseModel):
    username: str
    password: str
    mfa_code: Optional[str] = None  # If provided, used when MFA challenge appears
    mfa_method: Optional[str] = "sms"  # "email" or "sms" - which method to use for code delivery

class MfaRequest(BaseModel):
    code: str

class StatisticsTestRequest(BaseModel):
    cookie: str
    days_back: int = 1

class LoginResponse(BaseModel):
    success: bool
    cookies: Optional[str] = None
    error: Optional[str] = None
    mfa_required: Optional[bool] = None  # True when MFA needed - call POST /login/mfa with code

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "psegli-automation"}

@app.post("/test-statistics")
async def test_statistics(request: StatisticsTestRequest):
    """Test the Smart Energy requests used by the statistics updater."""
    if not request.cookie.strip():
        raise HTTPException(status_code=400, detail="cookie is required")
    if request.days_back < 1 or request.days_back > 365:
        raise HTTPException(status_code=400, detail="days_back must be between 1 and 365")

    end_date = datetime.now()
    start_date = end_date - timedelta(days=request.days_back)
    headers = {
        "Cookie": request.cookie,
        "Referer": "https://mysmartenergy.psegliny.com/Dashboard",
        "User-Agent": "Mozilla/5.0",
        "X-Requested-With": "XMLHttpRequest",
    }
    result = {
        "dashboard_status": None,
        "dashboard_url": None,
        "token_found": False,
        "chart_setup_status": None,
        "chart_setup_redirect": None,
        "chart_data_status": None,
        "series": [],
    }

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        async with session.get("https://mysmartenergy.psegliny.com/Dashboard") as response:
            dashboard_html = await response.text()
            result["dashboard_status"] = response.status
            result["dashboard_url"] = str(response.url)

        token_match = re.search(
            r"<input[^>]+name=['\"]__RequestVerificationToken['\"][^>]+value=['\"]([^'\"]+)",
            dashboard_html,
            flags=re.IGNORECASE,
        ) or re.search(
            r"<input[^>]+value=['\"]([^'\"]+)['\"][^>]+name=['\"]__RequestVerificationToken['\"]",
            dashboard_html,
            flags=re.IGNORECASE,
        )
        result["token_found"] = token_match is not None
        if not token_match:
            return result

        chart_request = {
            "__RequestVerificationToken": token_match.group(1),
            "UsageInterval": "5",
            "UsageType": "1",
            "jsTargetName": "StorageType",
            "EnableHoverChart": "true",
            "Start": start_date.strftime("%Y-%m-%d"),
            "End": end_date.strftime("%Y-%m-%d"),
            "IsRangeOpen": "False",
            "MaintainMaxDate": "true",
            "SelectedViaDateRange": "False",
            "ChartComparison": "0",
            "ChartComparison2": "0",
            "ChartComparison3": "0",
            "ChartComparison4": "0",
        }
        async with session.post("https://mysmartenergy.psegliny.com/Dashboard/Chart", data=chart_request) as response:
            result["chart_setup_status"] = response.status
            if response.content_type == "application/json":
                try:
                    payload = await response.json()
                    for item in payload.get("AjaxResults", []):
                        if item.get("Action") == "Redirect":
                            result["chart_setup_redirect"] = item.get("Value")
                except (TypeError, ValueError):
                    pass

        async with session.get("https://mysmartenergy.psegliny.com/Dashboard/ChartData") as response:
            result["chart_data_status"] = response.status
            if response.content_type == "application/json":
                try:
                    payload = await response.json()
                    series = payload.get("Data", payload).get("series", [])
                    result["series"] = [
                        {"name": item.get("name"), "points": len(item.get("data", []))}
                        for item in series
                    ]
                except (TypeError, ValueError):
                    pass

    return result

@app.post("/login", response_model=LoginResponse)
async def login(request: LoginRequest):
    """Login to PSEG and return cookies. If MFA is required, returns mfa_required=true - then POST to /login/mfa with the code."""
    global _mfa_session
    try:
        logger.info(f"Login attempt for user: {request.username}")
        
        # Clear any stale MFA session
        if _mfa_session:
            try:
                await _mfa_session.cleanup()
            except Exception:
                pass
            _mfa_session = None
        
        # Use direct PSEGAutoLogin to support two-phase MFA flow
        cookie_getter = PSEGAutoLogin(
            email=request.username,
            password=request.password,
            mfa_code=request.mfa_code,
            mfa_method=request.mfa_method or "sms",
            headless=not HEADED,
        )
        result = await cookie_getter.get_cookies()
        
        if result == "MFA_REQUIRED":
            _mfa_session = cookie_getter
            logger.info("MFA required - waiting for code via POST /login/mfa")
            return LoginResponse(
                success=False,
                mfa_required=True,
                error="PSEG requires multi-factor authentication. Check your email or phone for the verification code, then POST to /login/mfa with the code."
            )
        
        if result:
            logger.info("Login successful, cookies obtained")
            return LoginResponse(success=True, cookies=result)
        else:
            logger.warning("Login failed, no cookies returned")
            error_msg = getattr(cookie_getter, "last_error", None) or "Login failed"
            return LoginResponse(success=False, error=error_msg)
            
    except Exception as e:
        logger.error(f"Login error: {e}")
        return LoginResponse(success=False, error=str(e))

@app.post("/login/mfa", response_model=LoginResponse)
async def login_mfa(request: MfaRequest):
    """Complete login after MFA - provide the verification code from your email or SMS."""
    global _mfa_session
    if _mfa_lock.locked():
        return LoginResponse(
            success=False,
            error="MFA verification is already in progress. Wait for the first request to finish."
        )

    async with _mfa_lock:
        session = _mfa_session
        if not session:
            return LoginResponse(
                success=False,
                error="No MFA session in progress. Call POST /login first, then provide the code from your email or phone here."
            )

        try:
            logger.info("Completing MFA with provided code")
            cookies = await session.continue_after_mfa(request.code)

            if cookies:
                logger.info("MFA successful, cookies obtained")
                return LoginResponse(success=True, cookies=cookies)
            return LoginResponse(success=False, error="MFA verification failed - code may be invalid or expired")
        except Exception as e:
            logger.error(f"MFA error: {e}")
            return LoginResponse(success=False, error=str(e))
        finally:
            if _mfa_session is session:
                _mfa_session = None
            try:
                await session.cleanup()
            except Exception as cleanup_error:
                logger.warning(f"MFA cleanup failed: {cleanup_error}")

@app.post("/login-form", response_model=LoginResponse)
async def login_form(
    username: str = Form(...),
    password: str = Form(...),
    mfa_code: Optional[str] = Form(None),
    mfa_method: Optional[str] = Form("sms"),
):
    """Login endpoint that accepts form data."""
    return await login(LoginRequest(username=username, password=password, mfa_code=mfa_code, mfa_method=mfa_method))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
