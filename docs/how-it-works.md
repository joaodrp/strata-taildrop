# How it works

Detail behind the [README](../README.md): how the provider talks to Strata, how it decides who
can receive, and why a send is checked twice before anything leaves the machine.

## The provider protocol

Strata runs each registered provider as a child process and talks to it in newline-delimited
JSON over stdin and stdout ([Strata's provider docs](https://github.com/lgse/strata/blob/v1.1.1/docs/file-providers.md)).
It sends three kinds of request:

| Request | Taildrop answers |
| --- | --- |
| `query` | No badges. Taildrop has nothing to say about a file until you send it |
| `menu` | One `send-to` destination per device, which Strata groups under **Send to -> Taildrop** |
| `activate` | Starts the send and replies at once, so Strata shows a short notice instead of waiting |

`provider.json` registers the process. Strata runs its `command` as literal argv with no shell
and no `PATH` lookup, which is why the installer writes an absolute interpreter path.

## Who shows up

On a menu request the provider reads `tailscale status --json`, cached for five seconds because
Strata refreshes open menus that often. A peer is listed when all of these hold:

| Check | Why |
| --- | --- |
| `TaildropTarget` is `1` (available) | Tailscale reports the peer eligible to receive. Its verdict already folds in being online, ownership and a client that accepts files, so a tagged server, for example, is left out |
| It has a stable node ID | Needed to recognise the same device at activation, below |
| Its MagicDNS name is a usable label | Some clients report a `HostName` of `localhost`; the first label of the MagicDNS name is unique and readable |

The IPv4 address is preferred when a peer has both. An IPv6 target is bracketed, as
`tailscale file cp` requires.

## Folders, symlinks and special files

The menu is built only when every selected item is a regular file, checked without following
symlinks. A folder, a symlink, a socket or a device file gets no destinations: Taildrop sends
files, and a symlink could point somewhere the menu never showed you.

## Tickets

Each destination in the menu carries an opaque, random context token. The provider remembers
what it means:

```
token -> selection (exact paths, in order)
         identity of each file (device, inode, size, mtime, ctime)
         action id
         peer (stable node ID, name, address)
         expiry (two minutes)
```

An unchanged menu reuses its tokens, so a refresh does not rebuild it. Expired tokens are
dropped, and the cache holds at most 256.

## Activation

Choosing a device sends Strata's `activate` with that token. The provider:

1. **Consumes the ticket first.** A token works once, so a reply lost on the way back to Strata
   cannot send the files twice.
2. **Checks the request matches the ticket**: same paths, same action, not expired.
3. **Checks the device is still the same device.** A fresh `tailscale status` must list the same
   node ID at the same address, still available. A device that went offline or changed address
   since the menu opened is refused rather than guessed at.
4. **Opens each file and checks it is still the same file.** It opens with `O_NOFOLLOW`, then
   compares the open file's identity with the ticket. A file that was edited, replaced or
   swapped for a symlink is refused.
5. **Hands the open files to a detached sender** and replies "Sending N file(s) to device".

Step 4 is what makes the check hold. The sender never reopens a path; it reads the descriptors
opened and verified in step 4, so a different file put at that path afterwards cannot be sent
instead. The descriptors pin *which* file is sent, not its bytes: a program that edits the same
file in place while it is being sent changes what arrives, as with any copy.

Opening uses `O_NONBLOCK` as well, so the type check rejects a FIFO swapped in at a selected
path instead of blocking the open, and with it Strata.

## The sender

The sender is the same `provider.py`, started with `--send` in its own session, so it survives
Strata stopping the provider. It receives the verified descriptors through `pass_fds` and, for
each one, runs

```sh
tailscale file cp --name <original file name> - <target>:
```

with that descriptor as stdin. Then it shows a desktop notification with the result: sent, or
failed with Tailscale's own error. A background thread in the provider waits on the sender, so
finished senders do not linger as zombie processes.

Strata only learns that the send started. The notification is the only report of how it
ended, which keeps a slow transfer from holding up Strata's menus.

## Limits

- Up to 200 files per send, Strata's own cap for provider actions.
- At most 64 devices in the menu.
- Files only; Taildrop has no folder transfer.
- Notifications use `omarchy-notification-send` on Omarchy and `notify-send` elsewhere. With
  neither installed the send still happens, but nothing reports the result.
