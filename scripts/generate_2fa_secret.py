#!/usr/bin/env python3
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
