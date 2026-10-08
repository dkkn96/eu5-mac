# Testing and evidence

The repository's tests are standard-library `unittest` fixtures. They create
temporary app bundles and a small fake Wine archive. They do not access Steam,
the user's wrapper, game files, credentials, saves, network, or external
volumes.

The verified fixture checks are:

- default inventory is read-only;
- the pinned download path streams to a private temporary file, accepts a good
  mocked response, rejects a mismatch, and reuses an unchanged verified cache;
- Template 1.0.21, MoltenVK 1.4.1 evidence, EU5 app ID 3450310, build ID,
  and PE architecture are recognized;
- a detected EU5/Wine process blocks mutation;
- relative wrapper links, invalid backup placement, wrong-wrapper restores,
  process-inspection failure, special files, duplicate members, link
  descendants, and wrong archive checksums are rejected;
- apply preserves unrelated plist keys and registry bytes while backing up the
  original engine;
- restore returns the original engine and plist;
- a forced apply or plist-swap failure rolls back the engine and plist;
- already configured wrappers are handled idempotently.

Run:

```sh
python3 -m unittest discover -s tests -v
```

The local reference run is narrower than a release qualification. It provides
the following evidence:

- Apple M4 Pro, 48 GB RAM, macOS 27.0.1;
- Sikarugir Template 1.0.21 with MoltenVK 1.4.1 evidence;
- EU5 1.3.11, build 24187685, installed through Windows Steam;
- Gcenx Wine 11.13 selected by the wrapper;
- Vulkan/MoltenVK startup reached `MainMenu->Game` state 4;
- after resetting only the persisted graphics override object and restarting,
  the user reported simulation advancement for several in-game days without
  changing graphics settings.

The following remain pending: clean installation from an empty wrapper, a
normal Finder relaunch with no terminal environment, sustained campaigns,
save/reload, performance comparisons, and other Macs. A user-reported freeze
after changing an unspecified graphics quality or preset is still unresolved,
so this project does not claim graphics-setting stability.

The helper's apply/restore cycle has been tested with fixtures. An end-to-end
run of the helper on a fresh real wrapper remains pending.
