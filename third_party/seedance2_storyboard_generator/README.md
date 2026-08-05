# Seedance2 Storyboard Generator upstream lock

This integration uses the professional workflow from
`liangdabiao/Seedance2-Storyboard-Generator` as an upstream creative engine
instead of rewriting its prompt rules.

Pinned revision:

```text
17b9ca6dfac3e4a086a2874791ef19ae5aae3932
```

The upstream Skill and its five references are fetched into the local ignored
path `.local/upstream/Seedance2-Storyboard-Generator` by:

```bash
python -m src.apps.jp_drama.workflows.sync_seedance_storyboard_upstream
```

Every file is verified against the Git blob SHA recorded in
`upstream.lock.json`. The source files are not silently modified by this
repository.

The upstream README says that its content is for learning and reference. Keep
attribution and confirm redistribution/commercial-use rights before shipping a
redistributed copy. Generated Markdown projects can still be imported without
having the upstream source files present.
