# LaTeX source (ACM acmart, SIGGRAPH/TOG track)

Built against `acmart-primary` (user's local copy, Nov 2026). The class files
`acmart.cls` + `ACM-Reference-Format.bst` are copied here so the folder is
self-contained.

## Build

```bash
latexmk -pdf main.tex          # full pipeline incl. bibtex
```

Manual cycle if latexmk state gets stuck:

```bash
pdflatex main && bibtex main && pdflatex main && pdflatex main
```

## Document class

- Current: `\documentclass[acmtog,review,anonymous]{acmart}` — TOG review
  format, anonymized.
- Camera-ready: change to `[acmtog]{acmart}`.
- The acmart sample suggests `[manuscript,screen,review]{acmart}` for some
  submission portals; check the SIGGRAPH 2027 author kit when it drops.

## Conventions

- `\TODO{...}` and `\RESULT{...}` (red inline markers) mark unfinished
  content. **A `\RESULT` must only ever be replaced by a number produced by
  the experiments in `../../PLAN.md` (claim table C1–C7).**
- `references.bib`: entries with placeholder author fields (`{CAD-Llama
  authors}` etc.) must be completed from the actual papers before submission.
- CCS concepts in `main.tex` are provisional; regenerate with the ACM CCS
  tool.

## Status

| Section | State |
|---|---|
| Abstract | draft, results as `\RESULT` |
| 1 Introduction | full draft |
| 2 Related Work | full draft |
| 3 Problem Formulation | full draft |
| 4 Method | full draft (loop §4.5 describes the planned routing; code pending) |
| 5 Evaluation | skeleton: setup + metric protocol done, results placeholders |
| 6 Discussion/Conclusion | full draft |

## Bibliography TODO (before submission)

- [ ] Decide which **LLM4CAD version** to cite: `文献.md` lists the Design
  Theory and Methodology 2024 paper, but also links the ASME JCISE version.
  Check whether these are conference/journal versions and keep only the
  appropriate entry (currently the DTM 2024 version is used).
- [ ] Replace the remaining placeholder author list with authors verified
  from the original paper: `texttocad_vl`.
- [ ] Complete the partial author lists currently containing `and others`:
  `cadfs`, `cadrille`, `selfrefine`, `clarify_general`.
- [ ] Verify the titles, venues, years, and arXiv/DOI fields of the entries
  completed from `文献.md` against the original papers; the notes provide
  initials rather than full given names for several authors.
- [ ] After all replacements, rebuild with
  `pdflatex main && bibtex main && pdflatex main && pdflatex main` and check
  that no placeholder authors or BibTeX warnings remain.
