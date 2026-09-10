🇳🇱 [Nederlands](../nl/installation.md) | 🇬🇧 **English**

# Installation

**In one sentence:** install Alpha EMS Manager through HACS as a *custom repository*,
then add it as an integration.

## Why it works this way

Alpha EMS Manager is **not in the HACS default repository**. That means you add the
repository by hand first; after that it behaves like any other HACS integration,
updates included.

Submission to the default list follows once there is a stable 1.0 release. When that
happens, the steps below can get shorter.

---

## Before you start

Check the [Requirements](requirements.md) first. In particular: **Frank Quarter Prices
must already be working**, or you cannot add Alpha EMS at all.

---

## Installing through HACS

1. In Home Assistant, go to **HACS**.
2. Open the **⋮** menu (top right) → **Custom repositories**.
3. Enter:
   - **Repository:** `https://github.com/Bennie-JC/ha-alpha-ems-manager`
   - **Type:** `Integration`
4. Click **Add**.
5. Search HACS for **Alpha EMS Manager** and click **Download**.
   - This is a pre-release. Enable **Show beta versions** if `1.0.0-beta.56` is not
     offered.
6. **Restart Home Assistant.**
7. Continue with [Configuration](configuration.md).

## Installing manually

Possible, but you get no update notifications.

1. Download the source for the release you want from the
   [Releases page](https://github.com/Bennie-JC/ha-alpha-ems-manager/releases).
2. Copy the `custom_components/alpha_ems_manager/` directory into your Home Assistant
   `config/custom_components/` directory, so that
   `config/custom_components/alpha_ems_manager/manifest.json` exists.
3. **Restart Home Assistant.**
4. Continue with [Configuration](configuration.md).

---

## Adding the integration

After the restart:

**Settings → Devices & Services → Add Integration → Alpha EMS Manager**

You then work through four or five short steps. See [Configuration](configuration.md)
for every field individually.

---

## Updating

Update through HACS (or replace the directory for a manual install) and restart.

**Your learned history is preserved.** It lives in `.storage`, keyed per config entry,
and is not touched by an update. Your settings are kept too.

After an update it is worth reading the release notes: occasionally the meaning of a
published value changes, or a new setting appears.

---

## Upgrading from 0.1.0

**This cannot be done in place.**

The two configuration models share no keys, so an old entry cannot be migrated. Alpha
EMS recognises such an entry and refuses to load it, with a clear error, rather than
misreading it.

**What you do:**

1. Remove the integration.
2. Add it again.

The old storage file (`alpha_ems_manager_learning`) is left untouched and can be deleted
by hand.

⚠️ **Note:** learned history is tied to the config entry. A new entry starts with an
**empty** history, so the learning model begins again.

---

## Removing

Remove the integration through **Settings → Devices & Services**. That removes its
entities and the stored history for that entry.

If Alpha EMS was running in *Live* at the time, set **Control Mode** to **Off** first
and check that no dispatch is still running. The AlphaESS package's reset automation
also covers this, but closing down cleanly is better.

---

## Next

[Configuration](configuration.md) — every setting, field by field.
