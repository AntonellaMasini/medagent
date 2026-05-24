"""One-time setup: create Speech Engine + register Twilio number in ElevenLabs.

Run this once after setting ELEVENLABS_API_KEY, TWILIO_ACCOUNT_SID,
TWILIO_AUTH_TOKEN, and TWILIO_VOICE_NUMBER in your .env.

Usage:
    uv run python scripts/setup_speech_engine.py --ws-url wss://YOUR-NGROK.ngrok.io/ws

It will print the ELEVENLABS_AGENT_ID and ELEVENLABS_PHONE_NUMBER_ID
you need to add to your .env.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up Speech Engine for MedAgent")
    parser.add_argument(
        "--ws-url",
        required=True,
        help="Public WSS URL for the Speech Engine handler (e.g. wss://abc.ngrok.io/ws)",
    )
    parser.add_argument(
        "--name",
        default="MedAgent Booking",
        help="Name for the Speech Engine (default: MedAgent Booking)",
    )
    args = parser.parse_args()

    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        print("ERROR: ELEVENLABS_API_KEY not set in environment or .env")
        sys.exit(1)

    from elevenlabs import ElevenLabs
    from elevenlabs.types.speech_engine_config import SpeechEngineConfig
    from elevenlabs.types.tts_conversational_config_input import (
        TtsConversationalConfigInput,
    )

    client = ElevenLabs(api_key=api_key)

    voice_id = os.environ.get("ELEVENLABS_VOICE_ID", "ewn5JTa3lNPY8QVuZJi6")
    existing_id = os.environ.get("ELEVENLABS_AGENT_ID", "")

    tts_config = TtsConversationalConfigInput(
        voice_id=voice_id,
        model_id="eleven_flash_v2_5",
    )

    if existing_id and existing_id.startswith("seng_"):
        # --- Update existing Speech Engine ---
        print(f"Updating Speech Engine '{existing_id}' with ws_url={args.ws_url}")
        engine = client.speech_engine.update(
            existing_id,
            name=args.name,
            speech_engine=SpeechEngineConfig(ws_url=args.ws_url),
            tts=tts_config,
            language="es",
        )
        engine_id = engine.engine_id
        print("\n✓ Speech Engine updated!")
        print(f"  ELEVENLABS_AGENT_ID={engine_id}")
    else:
        # --- Create new Speech Engine ---
        print(f"Creating Speech Engine '{args.name}' with ws_url={args.ws_url}")
        engine = client.speech_engine.create(
            name=args.name,
            speech_engine=SpeechEngineConfig(ws_url=args.ws_url),
            tts=tts_config,
            language="es",
        )
        engine_id = engine.engine_id
        print("\n✓ Speech Engine created!")
        print(f"  ELEVENLABS_AGENT_ID={engine_id}")

    # --- Step 2: Register Twilio number ---
    twilio_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    twilio_token = os.environ.get("TWILIO_AUTH_TOKEN")
    twilio_number = os.environ.get("TWILIO_VOICE_NUMBER")

    if twilio_sid and twilio_token and twilio_number:
        print(f"\nRegistering Twilio number {twilio_number} in ElevenLabs...")
        from elevenlabs.conversational_ai.phone_numbers import (
            PhoneNumbersCreateRequestBody_Twilio,
        )

        phone_response = client.conversational_ai.phone_numbers.create(
            request=PhoneNumbersCreateRequestBody_Twilio(
                phone_number=twilio_number,
                label="MedAgent Voice",
                sid=twilio_sid,
                token=twilio_token,
            ),
        )
        phone_number_id = phone_response.phone_number_id
        print("✓ Phone number registered!")
        print(f"  ELEVENLABS_PHONE_NUMBER_ID={phone_number_id}")
    else:
        phone_number_id = None
        print(
            "\n⚠ Twilio credentials not found in .env — skipping phone registration."
            "\n  Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_VOICE_NUMBER and re-run."
        )

    # --- Summary ---
    print("\n" + "=" * 60)
    print("Add these to your .env:")
    print(f"  ELEVENLABS_AGENT_ID={engine_id}")
    if phone_number_id:
        print(f"  ELEVENLABS_PHONE_NUMBER_ID={phone_number_id}")
    print("=" * 60)


if __name__ == "__main__":
    main()
