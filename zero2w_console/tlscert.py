"""The console's own TLS certificate, made once, for the picture stream.

A board pins it exactly: it is handed over signed with the board's own token
(`fleet.media_offer`), so no certificate authority is involved and a board
trusts this console and nothing else. Made with the `openssl` command, because
Python's standard library can use a certificate but cannot make one.
"""
import os
import ssl
import subprocess

# The name a board asks for and the certificate answers to. Not a hostname
# anyone resolves: the board dials an address and checks this name.
NAME = "zero2w-console"


def paths(config_dir):
    base = os.path.join(config_dir, "tls")
    return os.path.join(base, "cert.pem"), os.path.join(base, "key.pem")


def ensure(config_dir):
    """(cert, key) paths, making them on first use. None without openssl."""
    cert, key = paths(config_dir)
    if os.path.isfile(cert) and os.path.isfile(key):
        return cert, key
    os.makedirs(os.path.dirname(cert), mode=0o700, exist_ok=True)
    base = ["openssl", "req", "-x509", "-newkey", "ec",
            "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
            "-keyout", key, "-out", cert, "-subj", "/CN=" + NAME,
            "-addext", "subjectAltName=DNS:" + NAME]
    # A board's clock starts at 2000-01-01 until it is told otherwise, and TLS
    # checks dates, so the certificate is valid from well before that. Older
    # openssl has no -not_before; the board sets its clock from the console
    # before it dials, which covers that case too.
    attempts = (base + ["-not_before", "19900101000000Z", "-not_after", "20991231235959Z"],
                base + ["-days", "36500"])
    old = os.umask(0o077)
    try:
        for argv in attempts:
            try:
                done = subprocess.run(argv, capture_output=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                return None
            if done.returncode == 0 and os.path.isfile(cert):
                os.chmod(key, 0o600)
                return cert, key
    finally:
        os.umask(old)
    return None


def server_context(cert, key):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


def der(cert):
    """The certificate as a board loads it."""
    with open(cert) as fh:
        return ssl.PEM_cert_to_DER_cert(fh.read())
