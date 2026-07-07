# MyNestra v2 — Build Prompt

You are building **MyNestra v2**, a personal multi-tenant household application, per
[`DESIGN.md`](DESIGN.md). **Read `DESIGN.md` in full first — it is authoritative.** The approved UI
mockups in [`mockups/`](mockups/) are the **normative visual reference**; visual deviation from them
is a bug. Open `mockups/preview.html` (or `mockups/index.html`) to see every screen, both themes,
and all five palettes.

## Mission

Deliver Contact Management (People, Families, Relationships) and Organizations on a disciplined,
consistent, component-driven UI — the thing v1 lacked. Grow module-by-module afterward.

## Working agreements

1. **Plan per phase, then get approval before coding it.** Present a short plan (scope, files,
   acceptance) for the phase and wait for the go-ahead. Don't run ahead into the next phase.
2. **Drive each phase to completion.** Finish, don't leave a phase half-built.
3. **Green gate to close a phase:** all tests pass **and** the tenant-isolation test passes.
4. **Components-only UI rule is a review criterion.** Feature templates compose cotton components;
   no ad-hoc styling. New patterns go into the component library + `/styleguide`, never inline.
5. **Conventional commits**, one logical change per commit.
6. **When `DESIGN.md` is ambiguous, stop and ask** — do not invent domain behavior.
7. Prefer the smallest correct change; match surrounding code style.

## UI gates (the anti-v1 mechanism)

- **Gate 1 — after P2:** the user reviews `/styleguide` and the app shell in the browser against the
  mockups. **No feature screen is built until this is approved.**
- **Gate 2 — after the first real screens (People list + Person detail in P4):** the user approves
  again before the pattern is replicated across the rest of the app.
- Throughout: `mockups/` is the source of truth. If a screen can't be built from existing
  components, add the component (and its `/styleguide` entry) first.

## Phases

Each phase lists **scope** and **✓ acceptance**.

### P0 — Scaffold
Repo layout; `docker compose` (web / db / tailwind / mailpit); Django project + settings split;
django-tenants wiring (SHARED vs TENANT apps, subfolder middleware, `TENANT_SUBFOLDER_PREFIX="t"`);
custom `User` (email login, no username); `.env.example`; `dev.ps1`/`Makefile`; a health page.
**✓** `docker compose up` serves a page; `db` reachable; Tailwind watcher compiles.

### P1 — Identity & tenancy
`Tenant`/`Domain`/`Membership`/`Invitation`; bootstrap command (first user + tenant + OWNER);
login/logout; password reset (email via Mailpit); invitation email + un-prefixed `/invite/<token>/`
accept (new vs existing user); tenant chooser + top-bar switcher; `/t/<slug>/` routing;
MembershipMiddleware (403 on non-member).
**✓** Two users, two tenants, cross-membership works; a non-member is denied; provisioning creates a
schema and seeds system data (§6).

### P2 — UI kit & shell  ← **Gate 1**
Design tokens in Tailwind config + CSS custom properties; self-hosted Inter; Lucide sprite;
light/dark with pre-paint script; **all cotton components** (§7.3) with every state; the five
**palettes** + `data-palette`; app shell (topbar, theme-matched sidebar + icon-rail collapse,
launcher chrome); **`/styleguide`** rendering every component/variant/state in both themes.
**✓** `/styleguide` renders everything in both themes and all palettes; shell matches mockups.
**Stop for Gate 1 approval.**

### P3 — Setup app
Categories (Person & Org) with system seed + **locks enforced**; relationship-type management
(P2P & P2O); members & invitations UI; **Appearance** (household palette = `Tenant.palette`,
per-user theme); Tenant profile; Recently-deleted scaffold.
**✓** New tenant has all seeds present and locked; Owner-only gates enforced; palette selection
persists and recolors the app.

### P4 — Contacts: People  ← **Gate 2**
`Person` CRUD; **PartialDate** widget + storage + display; ContactChannels & Addresses (unified,
exactly-one-owner CHECK); categories (M2M); ImportantDates; soft delete + restore + history;
People list (search, filter chips, sortable table, pagination); Person detail (Overview tab).
**✓** Full person lifecycle end-to-end; partial-date edge cases pass; list/detail match mockups.
**Stop for Gate 2 approval.**

### P5 — Relationships & Families
P2P engine with **gender-reciprocal label resolution** (stored once per pair; both sides render
correctly); relationship add/edit modal with live person search + label preview; Families + membership;
family page lists relationships between members; P2O links surfaced on Person detail.
**✓** Label-resolution matrix test passes for every seeded type × gender × side; families work.

### P6 — Organizations
`Organization` CRUD; `OrgIdentifier`s; `Branch`es (with own channels/addresses); P2O links
("key people"); categories; Org dashboard + list + detail + forms.
**✓** Org lifecycle end-to-end; filtering Organizations by system category **"Bank"** works
(the seam future modules rely on).

### P7 — Dashboards & polish
Launcher live counts; both app dashboards (upcoming birthdays incl. partial-year dates; recents);
Recently-deleted screens with restore; **hard-delete gating** by `ALLOW_HARD_DELETE`; empty states
everywhere; 403/404/500 pages.
**✓** Full walkthrough demo: create household → invite member → add people/orgs → relationships →
families → dashboards → soft-delete + restore.

## Definition of done

- **Per phase:** scope built; unit/view tests + **tenant-isolation test** green; UI composes
  components only; matches mockups; conventional commits.
- **Final:** all phases complete; `docker compose up` runs the full app; `/styleguide` complete;
  the P7 walkthrough passes end-to-end.

## Running tests & app

`dev.ps1 test` (pytest), `dev.ps1 up` (compose), `dev.ps1 migrate` / `seed` / `bootstrap` /
`createtenant`. On any `DESIGN.md` ambiguity: **ask, don't invent.**
