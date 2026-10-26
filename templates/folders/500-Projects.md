# Projects

Your own projects, outside school and work: one card per project saying what it is, where its code
and files live, its stack and its links. The project's working notes stay in the project's own
folder; the card is how the assistant finds the project from the vault.

A card starts with the lines the session start reads:

```
---
path: ~/code/garden
label: Garden
status: active
summary: Raised-bed planner and watering log for the back garden
---
```

`path` is the project's folder, absolute (`~` and forward slashes are fine). A session started in
that folder, or in one inside it, is that project: it shows the newest receipt whose first line
starts with `[Garden]` and the open tasks whose `project` is `Garden`. Without a card, a session
is matched by its folder's name. Either way the record and the tasks a session shows stay its own
project's (due tasks show everywhere; the vault's own record shows inside the vault). With
`"projects_root"` in `.brain/config.json` (the folder holding your project folders), a session in
the vault names each folder there that has no card yet.

Every session, in any folder, also lists your open projects, one line per card: its `label` (else
the file name), its `status` and its `summary` (else the first line of text under the heading), so
the assistant sees that Garden exists before it proposes building something new. `status` decides
the list:

- `active`: listed first.
- `waiting`: paused or not started; listed after the active ones.
- `done`, `archived` or anything else: left out.
- no `status`: listed, so older cards keep showing.

A card needs no `path` to be listed (a thesis with no code folder is fine); `path` and `label`
only decide which project a session is in. This folder note is never a card.

Example sub-folder: `Side Projects/`
