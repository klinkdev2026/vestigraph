# Files and history

## Save file versions

```console
python -m vestigraph --repo ./my-history init
python -m vestigraph --repo ./my-history checkpoint ./chip.gds --title initial
python -m vestigraph --repo ./my-history history
```

Each checkpoint has its own id. A checkpoint stores the bytes of the input file at that moment. Later edits to the source file do not change saved versions.

## Browse CLI history in the web service

Run these commands in local directories outside the product source tree and outside any Git repository you plan to publish. Keep the workspace, history root, and service state separate; they must not contain each other.

```console
python -m vestigraph service init --state ./service-state
python -m vestigraph service add-project --state ./service-state --name MyProject --workspace ./workspace --history-root ./history-storage
python -m vestigraph service add-history --state ./service-state --project PROJECT_ID --path ./my-history
python -m vestigraph serve --state ./service-state --open-browser
```

Use the `PROJECT_ID` returned by `add-project`. Existing history is attached read-only by default. Add `--writable` only when you want the browser service to append to that history. Store history outside product source and outside Git repositories.

## Automatic KLayout recording

After [KLayout integration](INSTALLATION.md), open a saved file and click **HIST**. Confirm the panel shows the current window and document as recording before editing. You can pause, resume, and name important states.

The recorded version is an exported copy from KLayout. It may differ from the original file on disk. Queued, in-progress, or failed captures are not saved versions. Automatic recording does not guarantee every intermediate editor state is captured.

## Import older files

Use the document import action to select local older files, review the preview plan, order, and target document, then confirm. Import order is not proof of original edit order. Duplicates, failures, and conflicts are reported in the panel.

## Preview and notes

Select a saved version to preview it and edit its name or notes. Large layouts are limited by time and memory budgets. Preview failure does not change the stored history file. Names, notes, and skill drafts are metadata, not the layout file itself.

Export and opening older versions in a new KLayout tab are covered in [Recovery](RECOVERY.md).
