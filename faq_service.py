import os
import logging

logger = logging.getLogger(__name__)

FAQ_DIR = os.path.join(os.path.dirname(__file__), "faq")


def load_general_faq() -> str:
    """Load the general FAQ (always included in system prompt)."""
    path = os.path.join(FAQ_DIR, "general.txt")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.warning("general.txt not found in faq/")
        return ""


def load_brand_faq(brand: str) -> str | None:
    """Load a brand-specific FAQ section. Returns None if not found."""
    safe_name = brand.lower().strip().replace(" ", "_")
    path = os.path.join(FAQ_DIR, f"{safe_name}.txt")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.info(f"No FAQ file for brand '{brand}'")
        return None


def list_available_brands() -> list[str]:
    """List all brand FAQ files available."""
    brands = []
    for fname in os.listdir(FAQ_DIR):
        if fname.endswith(".txt") and fname != "general.txt":
            brands.append(fname.replace(".txt", ""))
    return sorted(brands)
