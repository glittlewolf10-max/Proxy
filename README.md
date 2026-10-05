# Local Proxy

A small web proxy just for you. It runs on your own computer and you use it from a web page.

## Run it on a Chromebook

ChromeOS can't run Python directly, but its built-in **Linux** feature can. Set it up once:

1. Open **Settings → About ChromeOS → Developers → Linux development environment → Turn on**.
   Accept the defaults and wait a few minutes. A **Terminal** window opens when it's done.
2. Download `proxy.py` and `index.html` (for example with GitHub's **Code → Download ZIP**).
3. Open the **Files** app and drag both files from **Downloads** into **Linux files**.
4. In the Terminal app, run:

   ```
   python3 proxy.py
   ```

5. A Chrome tab opens at **http://127.0.0.1:8080**. If it doesn't, open that address yourself.

After that, open the Terminal app and run `python3 proxy.py` whenever you want to use the proxy.
Press `Ctrl+C` in the terminal (or close it) to stop.

**If Chrome says it can't reach the page:** stop the proxy and run `python3 proxy.py --host 0.0.0.0`.
Then open **http://penguin.linux.test:8080**. This is still private to your Chromebook,
because the Linux environment isn't reachable from your Wi-Fi network unless you turn on
port forwarding in Settings.

**If there's no "Linux development environment" option:** the Chromebook is managed by a
school or work account, which has turned Linux off. The proxy can't run there.

## Run it on Windows, Mac or Linux

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
