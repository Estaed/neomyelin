# Projects

Your own projects, outside school and work: one card per project saying what it is, where its code
and files live, its stack and its links. The project's working notes stay in the project's own
folder; the card is how the assistant finds the project from the vault.

A card starts with the two lines the session start reads:

```
---
path: ~/code/garden
label: Garden
---
```

`path` is the project's folder, absolute (`~` and forward slashes are fine). A session started in
that folder, or in one inside it, is that project: it shows the newest receipt whose first line
starts with `[Garden]` and the open tasks whose `project` is `Garden`. Without a card, a session
is matched by its folder's name; when no task or receipt carries that name, it shows only the due
tasks, never another project's record (the vault's own record shows inside the vault). With
`"projects_root"` in `.brain/config.json` (the folder holding your project folders), a session in
the vault names each folder there that has no card yet.

Example sub-folder: `Side Projects/`
