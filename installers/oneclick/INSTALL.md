# SAE Books — install it yourself in one minute

SAE Books Community is a free bookkeeping program that runs entirely on your
own computer. Your books never leave your machine. No account, no subscription,
no internet needed after the download.

## 1. Download

Grab the file for your computer from the
[latest release](https://github.com/saebooks/saebooks/releases):

| Your computer | File |
|---|---|
| Windows | `SAEBooks-windows-x64.exe` |
| Linux | `SAEBooks-linux-x86_64` |
| Mac (Apple Silicon) | `SAEBooks-macos-arm64` |

## 2. Run it

**Windows:** double-click `SAEBooks-windows-x64.exe`. The first time, Windows
SmartScreen shows "Windows protected your PC" — click **More info**, then
**Run anyway**. (The program is unsigned; that warning is expected.) A
"Starting SAE Books" window then appears while the first run unpacks — this
takes about a minute, once only. No need to click anything or start it again.

**Mac (Apple Silicon):** the first time you run it, macOS Gatekeeper blocks an
unsigned binary. Either clear the download flag with `xattr -d
com.apple.quarantine SAEBooks-macos-arm64`, or right-click the file in Finder,
choose **Open**, then **Open** again in the dialog. The app is ad-hoc signed,
not notarised with Apple; that warning is expected. Then, in a terminal:

```
chmod +x SAEBooks-macos-arm64
./SAEBooks-macos-arm64
```

**Linux:** make it executable and run it:

```
chmod +x SAEBooks-linux-x86_64
./SAEBooks-linux-x86_64
```

A black window opens and, a few seconds later, your web browser opens on the
SAE Books sign-in page. **Keep the black window open** — it IS the program.
Close it when you're finished for the day; your books are saved automatically.

## 3. Sign in

The first run creates a starter set of books so you can look around:

- Email: `you@example.com`
- Password: `change-me-now`

Change the password after signing in (or create your own account with
**Create an account** and ignore the starter books).

## Where are my books?

In one folder, on your machine:

- Windows: `C:\Users\<you>\AppData\Local\SAEBooks`
- Mac: `~/Library/Application Support/SAEBooks`
- Linux: `~/.local/share/SAEBooks`

Back that folder up and you've backed up your books. Delete it and you start
fresh.

## Troubleshooting

- **Browser didn't open?** Open it yourself and go to `http://127.0.0.1:18960`
  (the black window shows the exact address).
- **"Port in use" or a different address shown?** Another program was using
  the usual port; SAE Books picked a free one — use the address printed in the
  black window.
- **Windows Firewall asks for permission?** SAE Books only listens on your own
  machine (127.0.0.1). Allowing or denying makes no difference to normal use.
- **Started it twice?** No harm done — the second copy notices SAE Books is
  already running, opens your browser on it, and quits.
