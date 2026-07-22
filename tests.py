"""
Quick integration tests to verify services can connect and work.
Run with: python tests.py
"""
import asyncio
import sys
import os

# Ensure we load .env
from dotenv import load_dotenv
load_dotenv()

from config import settings
from sheets_service import SheetsService
from espocrm_service import EspoCRMService


async def test_config():
    """Test that all required config values are set."""
    print("=" * 50)
    print("TEST: Configuration")
    print("=" * 50)

    errors = []

    if not settings.GOOGLE_SHEETS_ID:
        errors.append("GOOGLE_SHEETS_ID is empty")
    else:
        print(f"  GOOGLE_SHEETS_ID: {settings.GOOGLE_SHEETS_ID[:10]}...")

    if not settings.GOOGLE_PRICES_SHEET_ID:
        errors.append("GOOGLE_PRICES_SHEET_ID is empty")
    else:
        print(f"  GOOGLE_PRICES_SHEET_ID: {settings.GOOGLE_PRICES_SHEET_ID[:10]}...")

    if not settings.ESPOCRM_URL:
        errors.append("ESPOCRM_URL is empty")
    else:
        print(f"  ESPOCRM_URL: {settings.ESPOCRM_URL}")

    if not settings.ESPOCRM_API_KEY:
        errors.append("ESPOCRM_API_KEY is empty")
    else:
        print(f"  ESPOCRM_API_KEY: {settings.ESPOCRM_API_KEY[:8]}...")

    if not settings.CHATWOOT_URL:
        errors.append("CHATWOOT_URL is empty")
    else:
        print(f"  CHATWOOT_URL: {settings.CHATWOOT_URL}")

    if not settings.OPENAI_API_KEY:
        errors.append("OPENAI_API_KEY is empty")
    else:
        print(f"  OPENAI_API_KEY: {settings.OPENAI_API_KEY[:8]}...")

    if errors:
        for e in errors:
            print(f"  FAIL: {e}")
        return False

    print("  PASS")
    return True


async def test_repairs_sheet():
    """Test connection to repairs Google Sheet."""
    print("\n" + "=" * 50)
    print("TEST: Repairs Sheet (Google Sheets)")
    print("=" * 50)

    try:
        svc = SheetsService()
        await svc.connect()

        if not svc._client:
            print("  FAIL: Could not connect to Google Sheets")
            return False

        records = await svc._fetch_all_records()
        print(f"  Fetched {len(records)} repair records")

        if records:
            cols = list(records[0].keys())
            print(f"  Columns: {cols[:5]}...")
            print("  PASS")
            return True
        else:
            print("  WARNING: Sheet is empty (0 records)")
            return True

    except Exception as e:
        print(f"  FAIL: {e}")
        return False


async def test_prices_sheet():
    """Test connection to prices Google Sheet."""
    print("\n" + "=" * 50)
    print("TEST: Prices Sheet (Google Sheets)")
    print("=" * 50)

    try:
        svc = SheetsService()
        await svc.connect()

        if not svc._client:
            print("  FAIL: Could not connect to Google Sheets")
            return False

        prices = await svc.get_all_prices()
        print(f"  Fetched {len(prices)} price records")

        if prices:
            sample = prices[0]
            print(f"  Sample: {sample['marca']} {sample['modelo']} - {sample['tipo_reparacion']} - {sample['precio']}")

            # Verify format_prices_for_prompt works
            prompt = svc.format_prices_for_prompt(prices)
            lines = prompt.split("\n")
            print(f"  Prompt generated: {len(lines)} lines")
            print("  PASS")
            return True
        else:
            print("  FAIL: No price records found")
            return False

    except Exception as e:
        print(f"  FAIL: {e}")
        return False


async def test_espocrm_ping():
    """Test EspoCRM API key authentication with a lightweight GET."""
    import httpx

    print("\n" + "=" * 50)
    print("TEST: EspoCRM API Key")
    print("=" * 50)

    try:
        url = settings.ESPOCRM_URL.rstrip("/")
        if not url or not settings.ESPOCRM_API_KEY:
            print("  FAIL: ESPOCRM_URL or ESPOCRM_API_KEY not configured")
            return False

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{url}/api/v1/App/user",
                headers={"X-Api-Key": settings.ESPOCRM_API_KEY},
            )

        if resp.status_code == 200:
            print(f"  Authenticated OK ({resp.status_code})")
            print("  PASS")
            return True
        else:
            print(f"  FAIL: status={resp.status_code} body={resp.text[:200]}")
            return False

    except Exception as e:
        print(f"  FAIL: {e}")
        return False


async def main():
    print("\nRunning integration tests...\n")

    results = []
    results.append(("Config", await test_config()))
    results.append(("Repairs Sheet", await test_repairs_sheet()))
    results.append(("Prices Sheet", await test_prices_sheet()))
    results.append(("EspoCRM API Key", await test_espocrm_ping()))

    print("\n" + "=" * 50)
    print("SUMMARY")
    print("=" * 50)

    all_passed = True
    for name, passed in results:
        status = "PASS" if passed else "FAIL"
        print(f"  {name}: {status}")
        if not passed:
            all_passed = False

    print()
    if all_passed:
        print("All tests passed!")
    else:
        print("Some tests failed.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
