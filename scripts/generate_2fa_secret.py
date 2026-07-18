#!/usr/bin/env python3
"""
generate_2fa_secret.py
Run this once, locally (NOT inside the Docker container - e.g. on the Unraid
host or your own machine), to generate a new TOTP secret for 2FA login.
Prints:
  1. The line to put in .env (TOTP_SECRET=...)
  2. The otpauth:// URI for manual entry into an authenticator app
  3. An ASCII QR code to scan (if the optional `qrcode` package is
     installed: `pip install qrcode`)

The secret never leaves this machine - nothing is sent anywhere. Afterwards:
add TOTP_SECRET to .env and restart the frontend container. Without a
TOTP_SECRET set, 2FA stays disabled.
"""

import pyotp

ISSUER = "Starlink Monitor"
ACCOUNT = "ben-ghosti"

secret = pyotp.random_base32()
uri = pyotp.totp.TOTP(secret).provisioning_uri(name=ACCOUNT, issuer_name=ISSUER)

print("=" * 60)
print("Add to .env:")
print(f"TOTP_SECRET={secret}")
print("=" * 60)
print("\nOr enter manually in your authenticator app:")
print(uri)
print()

try:
    import qrcode

    qr = qrcode.QRCode(border=1)
    qr.add_data(uri)
    qr.make()
    qr.print_ascii(invert=True)
except ImportError:
    print("(For a scannable QR code here: pip install qrcode)")
