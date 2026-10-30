# WorkSearch

Search the contents of everything in your Work folder: PDFs (including scanned ones), Word, Excel, PowerPoint, emails (.eml) and text files. File names and folder names are searchable too, so CAD files, photos and old `.doc`/`.xls` files can still be found by name.

WorkSearch runs on your own computer. It reads the QNAP over your normal network mount, keeps its index on the computer's own drive, and shows a search page in your browser at `http://127.0.0.1:8777`. Nothing is uploaded anywhere, and the page can only be opened from that computer.

## Install on the Mac

1. Unzip the WorkSearch folder (for example into Downloads).
2. Make sure the QNAP is mounted (you should see `/Volumes/Qnap/Work` in Finder).
3. Open **Terminal** and run:

   ```bash
   bash ~/Downloads/WorkSearch/install-mac.sh
   ```

   You can also double-click **Install WorkSearch.command**. macOS may block it because it was downloaded. If it does, use the Terminal command above.

The installer:
- uses Homebrew to install Python and Tesseract (the OCR engine). If Homebrew is missing, it offers to install it, which asks for your Mac password.
- asks which folder to search (default `/Volumes/Qnap/Work`)
- creates **WorkSearch.app** in your home folder's Applications (`~/Applications`). You can drag it to the Dock.
- optionally runs WorkSearch in the background from login, so the index stays current

The first time WorkSearch reads the QNAP, macOS may ask to allow access to files on a network volume. Click **Allow**.

## Install on Ubuntu

1. Mount the QNAP Work folder (see *Mounting the QNAP on Ubuntu* below).
2. In a terminal:

   ```bash
   bash ~/Downloads/WorkSearch/install-linux.sh
   ```

The installer uses `apt` to add Tesseract and the Python tools it needs (it asks for your password), asks which folder to search, adds **WorkSearch** to your app menu, and can start it in the background when you log in. You can also start it from a terminal with `worksearch`.

## Using it

- Type in the search box. Results update as you type and show the matching text with the page, slide or sheet it came from.
- Put quotes around words to match them as a phrase: `"three phase"`.
- Narrow results with the file-type buttons and the folder menu, or sort by **Newest first**.
- **Open** opens the file in its usual app. **Show in Finder** (or **Show in folder** on Linux) shows where it is. **Copy path** copies the full path.
- Keyboard: `/` jumps to the search box, the arrow keys move between results, and Enter opens the selected one.
- With an empty search box you see the most recently changed files.
- The bar at the bottom shows indexing progress. **Rescan now** checks for new and changed files right away. Otherwise WorkSearch checks every 4 hours while it is running. **Quit** stops it.

### The first scan

The first scan reads every file, so on 20 to 40 GB expect it to take a few hours. Files are read newest first, and you can search while it works. Scanned PDF pages are read by OCR in a second pass, which is the slowest part. The bottom bar shows how many pages are left. Later scans only read files that are new or changed.

### Files with limited search

The link at the bottom lists files that can only be found by name. These are old formats (`.doc`, `.xls`, `.ppt`, Outlook `.msg`), CAD and image files, files over 400 MB, and files that couldn't be read because they're password protected or damaged.

### If the QNAP isn't mounted

WorkSearch shows a warning and leaves the index alone, so you can still search everything that was indexed. Opening a file needs the QNAP to be mounted again.

## Settings

Settings are in `config.json` in the data folder:
- Mac: `~/Library/Application Support/WorkSearch/`
- Ubuntu: `~/.local/share/worksearch/`

| Setting | Default | Meaning |
|---|---|---|
| `root` | `/Volumes/Qnap/Work` | folder to search |
| `port` | `8777` | port for the search page |
| `rescan_hours` | `4` | how often to look for changes |
| `ocr_lang` | `eng` | Tesseract language(s), for example `eng+ita` (Mac: `brew install tesseract-lang`; Ubuntu: `sudo apt install tesseract-ocr-ita`) |
| `ocr_workers` | `0` (auto) | how many pages to OCR at once |
| `max_extract_mb` | `400` | larger files are indexed by name only |

Quit WorkSearch and reopen it after changing a setting. To search a different folder, run the installer again. The index, `worksearch.log` and `server.log` are in the same data folder.

## Mounting the QNAP on Ubuntu

Mounting from the Files app (*Other Locations → smb://qnap/Work*) works, but the folder ends up under a long `/run/user/…/gvfs/…` path that changes if the share is mounted differently. A fixed mount is more reliable:

```bash
sudo apt install cifs-utils
sudo mkdir -p /mnt/qnap/Work
```

Then add a line like this to `/etc/fstab`. Use your QNAP's address, share name and a credentials file:

```
//192.168.1.50/Work  /mnt/qnap/Work  cifs  credentials=/home/YOU/.qnap-cred,uid=YOU,iocharset=utf8,_netdev,nofail  0  0
```

## Uninstall

- Mac: `bash ~/Library/Application\ Support/WorkSearch/app/uninstall-mac.sh`
- Ubuntu: `bash ~/.local/share/worksearch/app/uninstall-linux.sh`

Add `--purge` to also delete the index and settings. Your own files are never changed or deleted. WorkSearch only reads them.
