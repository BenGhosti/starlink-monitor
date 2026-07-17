#!/usr/bin/env python3
"""
generate_2fa_secret.py
Einmalig lokal ausfuehren (NICHT im Docker-Container, sondern z.B. auf dem
Unraid-Host oder dem eigenen Rechner), um einen neuen TOTP-Secret fuer die
2FA-Anmeldung zu erzeugen. Gibt aus:
  1. Die Zeile fuer .env (TOTP_SECRET=...)
  2. Die otpauth://-URI zum manuellen Eintragen in eine Authenticator-App
  3. Einen ASCII-QR-Code zum Scannen (falls das optionale `qrcode`-Paket
     installiert ist: `pip install qrcode`)

Der Secret verlaesst nie diesen Rechner - es wird nichts ins Netz geschickt.
Danach: TOTP_SECRET in die .env eintragen und den frontend-Container neu
starten. Ohne gesetzten TOTP_SECRET bleibt 2FA deaktiviert.
"""

import pyotp

ISSUER = "Starlink Monitor"
ACCOUNT = "ben-ghosti"

secret = pyotp.random_base32()
uri = pyotp.totp.TOTP(secret).provisioning_uri(name=ACCOUNT, issuer_name=ISSUER)

print("=" * 60)
print("In .env eintragen:")
print(f"TOTP_SECRET={secret}")
print("=" * 60)
print("\nOder manuell in der Authenticator-App eintragen:")
print(uri)
print()

try:
    import qrcode

    qr = qrcode.QRCode(border=1)
    qr.add_data(uri)
    qr.make()
    qr.print_ascii(invert=True)
except ImportError:
    print("(Fuer einen scanbaren QR-Code hier: pip install qrcode)")
