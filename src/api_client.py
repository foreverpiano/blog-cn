import time
import httpx
from src.config import OPENROUTER_API_KEY, OPENROUTER_MODEL, OPENROUTER_API_URL


class OpenRouterClient:
    def __init__(
        self,
        api_key: str = OPENROUTER_API_KEY,
        model: str = OPENROUTER_MODEL,
        max_retries: int = 3,
        requests_per_minute: int = 30,
    ):
        if not api_key:
            raise ValueError(
                "OPENROUTER_API_KEY not set. "
                "Copy .env.example to .env and add your key."
            )
        self.api_key = api_key
        self.model = model
        self.max_retries = max_retries
        self.min_interval = 60.0 / requests_per_minute
        self._last_request_time = 0.0
        self.client = httpx.Client(timeout=300)

    def _rate_limit(self):
        elapsed = time.time() - self._last_request_time
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request_time = time.time()

    def chat(self, system_prompt: str, user_message: str) -> str:
        """Send a chat completion request and return the response text."""
        self._rate_limit()

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        for attempt in range(self.max_retries):
            try:
                resp = self.client.post(
                    OPENROUTER_API_URL,
                    json=payload,
                    headers=headers,
                )

                if resp.status_code == 429:
                    wait = 2 ** (attempt + 2)
                    print(f"  Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                    continue

                if resp.status_code == 402:
                    raise ValueError("Insufficient credits on OpenRouter account")

                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]

            except httpx.HTTPStatusError as e:
                if attempt < self.max_retries - 1:
                    wait = 2 ** (attempt + 1)
                    print(f"  HTTP {e.response.status_code}, retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    raise

        raise RuntimeError("Max retries exceeded")

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
