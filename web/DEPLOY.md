# Memory Phone message app: deploy guide

Guests open a link (or scan a QR code), type their name, pick a year, tap
Record, and send. The phone checks for new messages every hour.

About an hour, all on your laptop.

---

## 1. Install Fly's tool and log in (5 min)

```
brew install flyctl      # on Mac. Other systems: see fly.io/docs/flyctl/install
fly auth login
```

A browser window opens. Log in with your existing Fly account.

## 2. Name your app (2 min)

Open `fly.toml` in this folder (TextEdit is fine). Change the first line to a
unique name, for example:

```
app = "memory-phone-yourname"
```

Your site will be `https://memory-phone-yourname.fly.dev`. Save the file.

## 3. Create the app and its storage (5 min)

In Terminal, in this folder (swap in your app name):

```
cd web
fly apps create memory-phone-yourname
fly volumes create phone_data --region sin --size 1 -a memory-phone-yourname
```

If it warns about having only one volume, type `y`. One is right for this.

## 4. Make three secret keys (5 min)

Run this three times and copy each result somewhere safe (Notes or your
password manager):

```
openssl rand -hex 16
```

- the first is the **guest key** (goes in the link you share)
- the second is the **phone key** (only the Pi knows it)
- the third is the **admin key** (only you know it)

Then set them, pasting your three keys:

```
fly secrets set GUEST_KEY=first-key PHONE_KEY=second-key ADMIN_KEY=third-key -a memory-phone-yourname
```

## 5. Deploy (5 to 10 min the first time)

```
fly deploy
```

It ends with a line showing your app's address. Done when it says the
machine is in a good state.

## 6. Test on two real phones (15 min)

Your links (swap in your app name and keys):

| Link | Who it's for |
|---|---|
| `https://memory-phone-yourname.fly.dev/r/GUEST_KEY` | Guests, family, friends |
| `https://memory-phone-yourname.fly.dev/admin?key=ADMIN_KEY` | Only you |

1. Open the guest link on an **iPhone in Safari**: name, year, Record, Stop, listen, Send.
2. Do the same on an **Android in Chrome**.
3. Open the admin page. Both messages should be there; press play on each.
4. Delete the test messages on the admin page.

**Important:** links opened inside WhatsApp or Instagram open in their
built-in browser, which often blocks the microphone. If Record does nothing,
tap the menu and choose **Open in Safari / Open in Chrome**. A QR code scanned
with the camera opens the real browser, so the party is fine.

## 7. QR code for the party (5 min)

```
brew install qrencode
qrencode -o party-qr.png -s 12 -m 2 "https://memory-phone-yourname.fly.dev/r/GUEST_KEY"
open party-qr.png
```

Print it on a small card: *"Leave a message. Scan, record, send."*

## 8. Connect the phone (10 min)

From your Mac, copy the sync files to the Pi:

```
scp phone/sync.py phone/sync.service phone/sync.timer pi@memoryphone.local:~/memory-phone/
```

Then on the Pi (`ssh pi@memoryphone.local`):

```
cd ~/memory-phone
nano sync.env
```

Type these two lines with your app name and **phone key**, then Ctrl+O, Enter, Ctrl+X:

```
SERVER_URL=https://memory-phone-yourname.fly.dev
PHONE_KEY=second-key
```

Lock it down, test once, then switch on the automatic sync:

```
chmod 600 sync.env
set -a; source sync.env; set +a; python3 sync.py
sudo cp sync.service sync.timer /etc/systemd/system/
sudo systemctl enable --now sync.timer
sudo systemctl restart phone
```

The test should say `Sync done`. Record a test message, wait up to an hour
(or run `python3 sync.py` again), and dial its year.

---

## Good to know

- **Approval:** messages go straight to the phone. To approve each one first:
  `fly secrets set AUTO_APPROVE=0 -a memory-phone-yourname`. New ones then wait on
  the admin page until you press Approve.
- **Deleting:** delete on the admin page and it disappears from the phone at the next sync.
- **Future dates:** a guest can pick a date under *When should they hear it?*. The message
  stays hidden until then (it unlocks at 8am Singapore time that day; change this in `app.py` for your time zone).
- **Cost:** the app sleeps when idle and wakes in a few seconds. Expect about
  $1 to $3 a month.
- **Logs if something breaks:** `fly logs -a memory-phone-yourname`
- **Backup:** every message is on the Fly volume and on the phone's SD card.
