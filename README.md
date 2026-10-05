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

## Run it on your phone, use it on your Chromebook

The phone runs the proxy; the Chromebook just opens a web page. Both devices must be on
the **same Wi-Fi**, or connect the Chromebook to your **phone's hotspot**.

### Android phone (recommended)

1. Install **Termux** from F-Droid (f-droid.org) or its GitHub releases page. The Play Store version may be out of date.
2. Download `proxy.py` and `index.html` on your phone.
3. Open Termux and run these once:

   ```
   pkg install python
   termux-setup-storage
   cp ~/storage/downloads/proxy.py ~/storage/downloads/index.html ~
   ```

   (`termux-setup-storage` asks for permission to read your files; tap Allow.)

4. Start the proxy:

   ```
   python proxy.py --share
   ```

   It prints a **key** and a link like `http://192.168.1.23:8080/?key=abcd2345`.

5. On the Chromebook, type that link into Chrome. The key is saved, so next time you only need
   `http://192.168.1.23:8080`.

Keep Termux running while you browse. Pull down the Termux notification and tap
**Acquire wakelock** so Android doesn't pause it when the screen turns off.

### iPhone

Install **a-Shell** from the App Store, copy both files into it with the Files app, and run
`python3 proxy.py --share`. iOS pauses apps you switch away from, so a-Shell must stay open
on screen while you browse. Android works much better for this.

### If the printed link doesn't load

- Check both devices are on the same Wi-Fi network (guest networks and school or work Wi-Fi often block devices from reaching each other).
- Using the hotspot? The phone's address is the **gateway** shown on the Chromebook under
  **Settings → Network → (your hotspot) → Network**. Open `http://<that address>:8080`.
- Find the phone's address on Android under **Settings → About phone → Status → IP address**.

### Keeping it private

In `--share` mode anyone on the same Wi-Fi could find the proxy, so it asks for the key first.
For the most privacy, use your phone's hotspot: then your Chromebook is the only device on the network.

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
