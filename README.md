# Local Proxy

A small web proxy just for you. It runs on your own computer and you use it from a web page.

## Run it

1. Install Python 3 (already there on macOS and Linux; on Windows get it from python.org).
2. Put `proxy.py` and `index.html` in the same folder.
3. Run:

   ```
   python3 proxy.py
   ```

   (On Windows: `py proxy.py`, or double-click `proxy.py`.)

4. Your browser opens at **http://127.0.0.1:8080**. Type a URL or a search into the bar.

Press `Ctrl+C` in the terminal to stop. Use another port with `python3 proxy.py --port 9000`.

## Notes

- It only listens on `127.0.0.1`, so other devices can't use it.
- You need no extra installs. It uses only the Python standard library.
- Simple and medium sites work well. Heavy single-page apps (YouTube, Google Docs, sites with logins and captchas) may break.
- Every proxied site runs on the same local address, so sites could read each other's cookies and storage. Don't sign in to important accounts (bank, email) through it.
