# MyNestra v2 — Design

> Authoritative design document. Read this fully before writing any code.
> The phased build plan lives in [`PROMPT.md`](PROMPT.md). The approved UI mockups in
> [`mockups/`](mockups/) are the **normative visual reference** — visual deviation from them is a bug.

MyNestra is a personal, multi-tenant household application: a calm, well-kept home for the people
and organizations a household keeps in touch with. It is built **module by module**. Module 1 is
**Contact Management** (People, Families, Relationships) and **Organizations**. The overriding goal
of v2 is a **disciplined, consistent UI system** — the thing v1 lacked.

---

## 1. Vision & principles

- **Personal, multi-tenant, module-scalable.** One deployment serves many households (tenants).
  Each household's data is isolated in its own database schema. New capability arrives as new
  *modules* (Django apps), each surfacing as a tile on the launcher.
- **UI discipline is a first-class requirement.** v1 grew inconsistent because Tailwind was used
  without a component layer (per-page utility soup → drift), a 7-swatch theme picker multiplied
  every design decision, forms were default-ish, and there was no empty/edge-state design and no
  UI review gate. **The aesthetic didn't fail — ungoverned freedom failed.**
- **The anti-v1 rule (enforceable in review):** feature pages **compose components only**. No
  ad-hoc styling on feature templates. New visual patterns are added to the component library and
  the `/styleguide`, never inline on a page.
- **Structured richness.** Richness comes from *structure and depth* — tiles, cards, icons,
  shadows — on a ~90% neutral foundation. **Color appears only where it carries meaning:** status
  badges, KPI deltas, the primary action, active navigation, category chips. One accent at a time.

---

## 2. Tech stack

Pin the latest compatible versions at scaffold time; verify `django-tenants` ↔ Django ↔ psycopg
compatibility then. Target baseline:

| Concern | Choice |
|---|---|
| Language / runtime | Python 3.12+ |
| Framework | **Django 5.2 LTS**, Django Templates (no DRF in module 1) |
| Multi-tenancy | **django-tenants** (schema-per-tenant) with `TenantSubfolderMiddleware` |
| Database | **PostgreSQL 17** via **psycopg 3** |
| Components | **django-cotton** (HTML component library) |
| Audit | **django-simple-history** on all tenant models |
| Front-end interactivity | **htmx 2** + **Alpine.js 3**, both vendored as static (no JS build step) |
| CSS | **Tailwind CSS 4** via the standalone CLI watcher (container); design tokens as CSS custom properties |
| Font | **Inter**, self-hosted (subset woff2). Mockups embed it as a data-URI; production self-hosts under `static/`. |
| Icons | **Lucide** (one set), delivered as an inline SVG sprite; one stroke weight, two sizes |
| Testing | **pytest-django** + **factory-boy** |
| Email (dev) | **Mailpit** (SMTP + web UI) |
| Task runner | `dev.ps1` (Windows-first) with equivalent `make` targets |

No REST API, i18n, notifications, billing, reports, or global search in module 1 (see §12).

---

## 3. Architecture

### Schema-per-tenant

- **SHARED_APPS** (the `public` schema): `django_tenants`, the custom `users` app, the `tenants`
  app (Tenant, Domain, Membership, Invitation), Django contrib as required.
- **TENANT_APPS** (per-tenant schemas): `contacts`, `organizations`, `relationships`, `families`,
  `setup` (catalogs, categories, recently-deleted), plus `simple_history`.
- A model that must exist in both lives in `SHARED_APPS` ∩ `TENANT_APPS` per django-tenants rules;
  keep the split clean — identity/tenancy in public, everything household-owned in tenant schemas.

### Routing — subfolder tenants

- `TENANT_SUBFOLDER_PREFIX = "t"`. Tenant URLs are `/t/<slug>/...` resolved by
  `TenantSubfolderMiddleware`; `slug` **is** the PostgreSQL schema name.
- **Public (no tenant):** `/` (tenant chooser / landing when authenticated), `/login/`,
  `/logout/`, `/password-reset/...`, and the **un-prefixed** `/invite/<token>/` accept route.
- **Tenant-scoped:** `/t/<slug>/` (launcher), `/t/<slug>/contacts/...`,
  `/t/<slug>/organizations/...`, `/t/<slug>/setup/...`.

### Request lifecycle

```
request → TenantSubfolderMiddleware (resolve slug → set search_path to schema)
        → AuthenticationMiddleware
        → MembershipMiddleware (assert request.user ∈ this tenant; else 403)
        → view (queries run inside the tenant schema; cross-schema access impossible)
```

### Tenant provisioning

Creating a household is a transaction: **create schema → run tenant migrations → seed system data**
(§6) → create the founding `Membership` (role OWNER). Exposed via a management command
(`create_tenant`) and, later, an in-app "create household" flow. Seeding is idempotent and marks
seeded rows `is_system=True` (locked).

---

## 4. Identity & access

Invite-only. Public sign-up is disabled. The first account is created by a management command; all
others join through tokened invitation links. Password reset is by email.

### Models (public schema)

**User** — custom, `AbstractBaseUser` + `PermissionsMixin`.
| Field | Notes |
|---|---|
| `email` | **USERNAME_FIELD**, unique, citext-normalized. No username. |
| `full_name` | display name |
| `password` | Django hashing |
| `theme` | `light` \| `dark` \| null → null inherits system/household default (per-user preference) |
| `default_tenant` | FK Tenant, nullable — where the chooser lands first |
| `is_active`, `is_staff`, timestamps | standard |

**Tenant** (django-tenants `TenantMixin`) — `name`, `slug` (= schema), `logo`,
`palette` (household accent, default `teal`; see §7), `created_on`.
**Domain** (django-tenants `DomainMixin`) — as required by subfolder routing.
**Membership** — `user` FK, `tenant` FK, `role` (`OWNER` | `MEMBER`; stored as a short string so
new roles never require a migration), `joined_at`. Unique `(user, tenant)`.
**Invitation** — `email`, `tenant` FK, `role`, `token` (unguessable), `invited_by`, `status`
(`PENDING` | `ACCEPTED` | `REVOKED` | `EXPIRED`), `expires_at`, `created_at`.

### Flows

- **Bootstrap:** `create_superuser`-style command creates the first User + first Tenant + OWNER
  Membership.
- **Invite:** an Owner invites `email` + role → emailed `/invite/<token>/`. Accepting:
  *new user* sets name + password then joins; *existing user* (matched by email) just joins. Tokens
  expire and are single-use.
- **Tenant chooser & switcher:** after login, a user with >1 membership sees a chooser; the top-bar
  tenant switcher changes household in-session.
- **Owner-only gates:** Setup, invitations, appearance-for-household, danger zone, hard delete.

---

## 5. Data model (tenant schema)

All tenant models: `django-simple-history` registered (who/when/what audit), **soft delete** via a
`deleted_at` timestamp + a default manager that hides soft-deleted rows (`all_objects` sees them),
`created_at`/`updated_at`.

### PartialDate pattern (used everywhere a real-world date may be incomplete)

Stored as **three nullable smallints**: `<name>_year`, `<name>_month`, `<name>_day`. Implemented as
a reusable abstract mixin / composite value object with:
- **Validation:** month ∈ 1–12, day valid for month/year, `day` requires `month`, `month` may stand
  alone, `year` may stand alone.
- **Display helper:** renders `14-Mar-1974`, `14-Mar-XXXX` (no year), `XX-Mar-1974` (no day),
  `XX-XX-1974` (year only). `XX` for missing day/month, `XXXX` for missing year.
- **Upcoming-dates query:** "birthdays/anniversaries in the next N days" that works when `year`
  is null (compares month/day only) and skips fully-unknown month/day.

### Contacts

**Person**
| Field | Type / notes |
|---|---|
| `first_name`, `middle_name`, `last_name` | first + last effectively required; middle optional |
| `preferred_name` | shown across the app when set |
| `pronouns` | free text |
| `gender` | enum `M` \| `F` \| `O` \| `U` (unspecified) |
| `dob_*` | PartialDate |
| `is_deceased`, `dod_*` | flag + PartialDate date of death |
| `marital_status` | enum (single/married/widowed/divorced/…); extensible |
| `anniversary_*` | PartialDate |
| `occupation`, `education` | free text |
| `blood_group` | enum incl. `Unknown` |
| `dietary` | free text / short list |
| `languages` | list (array field or through table) |
| `photo` | image, nullable (initials avatar fallback) |
| `notes` | text |

**ImportantDate** — `person` FK, `label` (free text, e.g. "Retirement"), `date_*` PartialDate.
A person has any number. (Birthday & anniversary live on Person; ImportantDate is for the rest.)

### Organizations

**Organization** — `name`, `display_name`, `logo`, `website`, `notes`.
**OrgIdentifier** — `organization` FK, `type` (e.g. GST/Tax ID/Registration), `value`. Many per org.
**Branch** — `organization` FK, `name`, `is_primary`. A branch carries its own channels/addresses.

### Categories

**Category** — `name`, `kind` (`PERSON` | `ORG`), `color` (one of the curated chip tints), `is_system`
(locked; lock icon in Setup). Flat (no hierarchy). Many-to-many to Person and Organization
(separate through tables per kind). Multiple categories per contact.

### Contact info (unified, attachable to four owner types)

**ContactChannel** — `type` (`email` | `phone` | `whatsapp` | `url` | …), `label`, `value`,
`is_primary`. **Address** — structured parts (`line1`, `line2`, `city`, `region`, `postal_code`,
`country`), `label`, `is_primary`.
Each has **exactly one** of four nullable owner FKs — `person` | `organization` | `branch` |
`family` — enforced by a **DB CHECK constraint** (exactly-one-non-null). Any number per owner.

### Relationships

**RelationshipType (P2P)** — `code`, `is_symmetric`, and six label fields:
`a_label_m/f/n` and `b_label_m/f/n` (side-A and side-B labels by the *other* party's gender),
`is_system`. Seeded catalog + tenant-custom.
**PersonRelationship** — `person_a` FK, `person_b` FK, `type` FK, `note`. **Stored once per pair.**
Unique on the unordered pair + type. **Label resolution:** the label shown for person X viewing
person Y is derived from the type, which side X is on, and Y's gender (e.g. a stored
parent–child edge renders "Father"/"Son" automatically; symmetric types like spouse/sibling/friend
use the same label both ways). See the label-resolution matrix test (§10).

**PersonOrgRelationshipType (P2O)** — `code`, `label`, `is_system`. Seeded locked types + custom.
**PersonOrgRelationship** — `person` FK, `organization` FK, `type` FK, `role_note`,
`from_*`/`to_*` PartialDates. This is the "key people" feature on the Org side and the
"organizations" list on the Person side.

### Families

**Family** — `name`, `photo`, `notes`, optional address (via unified Address). No member roles.
**FamilyMembership** — `family` FK, `person` FK. Unique `(family, person)`. A person ∈ many families.
The family page **lists the interpersonal relationships between its members** (derived from
PersonRelationship), rather than storing roles.

### Soft delete & recently-deleted

Default managers exclude `deleted_at`. A **Recently deleted** area in Setup lists soft-deleted rows
(by type) with **restore**. **Hard delete** is gated by `ALLOW_HARD_DELETE`: dev (`=1`) shows a
one-click permanent delete; prod (`=0`) permits permanent deletion only from Recently-deleted with
explicit confirmation.

---

## 6. Seed catalogs (per tenant, on provisioning; `is_system=True`, locked)

Future modules depend on these system rows (e.g. Banking filters Organizations by category "Bank").

- **Organization categories:** Bank, Hospital/Clinic, Pharmacy, School/College, Insurance,
  Government, Employer, Utility, Merchant/Store, Religious, Club/Association.
- **Person categories:** Doctor, Lawyer, Accountant, Agent/Advisor, Teacher, Household Help,
  Friend of family.
- **P2P relationship types** (each with M/F/neutral labels on both sides): parent–child, spouse
  (sym), sibling (sym), grandparent–grandchild, uncle/aunt–nephew/niece, cousin (sym),
  parent-in-law–child-in-law, sibling-in-law (sym), friend (sym), colleague (sym), neighbour (sym).
- **P2O relationship types:** Customer, Account Holder, Employee, Patient, Student, Member, Owner,
  Service-Provider Contact.

Tenants may add their own categories/types; system rows cannot be edited or deleted (only hidden if
we later add hiding).

---

## 7. UI system

The mockups in [`mockups/`](mockups/) are normative. This section states the rules they embody.
`mockups/app.css` is the reference token + component sheet; production re-expresses the same tokens
in the Tailwind config and as cotton components.

### 7.1 Design tokens (CSS custom properties)

- **Neutral scale** (~90% of every screen): cool slate with a faint accent bias — `--bg`,
  `--surface`, `--surface-2/3`, `--border`, `--border-strong`, `--text`, `--text-strong`,
  `--text-muted`, `--text-subtle`.
- **Accent (one at a time):** `--accent`, `--accent-hover`, `--accent-fg`, `--accent-text`,
  `--accent-soft`, `--accent-soft-fg`, `--ring`.
- **Semantic (fixed across all palettes):** `--success`, `--warning`, `--danger`, `--info`, each
  with a `-soft` background and `-soft-fg` foreground.
- **Category chip tints:** a small curated set (teal, blue, violet, amber, rose, emerald, sky,
  slate, orange, fuchsia) — used only for category chips; independent of the accent.
- **Scales (fixed):** radii (`--r-sm 7`, `--r 10`, `--r-md 12`, `--r-lg 16`, full), shadows
  (`xs/sm/base/lg`), spacing (4-px base), type (`--fs-xs 11.5` … `--fs-4xl 34`), weights 400/500/600/700.
- **Chrome dims:** topbar 57px, sidebar 256px (collapsed 70px icon rail).

### 7.2 Theming (light/dark) + palettes

- **Light + dark** are both fully designed (not inverted). Class/attribute strategy:
  `data-theme="light|dark"` on `:root`; every token is redefined for dark. A **pre-paint inline
  script** in `<head>` sets the theme before first paint (no flash). **Theme is a per-user
  preference** (`User.theme`); null inherits the household/system default.
- **Palettes (curated, selectable).** Five complete, pre-tuned accents ship: **Teal (default)**,
  **Indigo**, **Blue**, **Violet**, **Graphite**. A palette is a single vetted token block defining
  the accent family for *both* light and dark; components read `--accent*` only, so adding/removing a
  palette touches **zero components**. The **household palette is chosen by an Owner in
  Setup → Appearance** (`Tenant.palette`, default `teal`), applied as `data-palette` on `:root`.
  **This is not a free color picker** and the four semantic colors never change — this is the
  disciplined replacement for v1's failed 7-swatch picker (one accent at a time, curated set only).

### 7.3 Cotton component inventory (build these FIRST; every one in `/styleguide`)

Each component has designed **default, hover, focus, disabled, empty, loading, and error** states as
applicable. Props/slots in parentheses.

`button` (variant: primary/default/ghost/danger; size sm/md; icon-only; leading/trailing icon) ·
`badge` (semantic + neutral + lock; soft style) · `chip` (category tint; removable) ·
`filter-chip` (selectable, active, count) · `card` (head/title/actions/body/foot) ·
`stat-tile` (icon tint, value, label, delta up/down) · `app-tile` (compact launcher infolet: glyph,
name, description, metrics strip; "coming soon" variant) ·
`avatar` (initials fallback, photo, sizes sm→xl, square variant, group + overflow) ·
`page-header` (breadcrumb, title, subtitle, actions) · `field` (label, required, help, error) with
inputs `text/select/textarea` · `partial-date-input` (day / month-select / year; XX/XXXX aware) ·
`toggle`/`switch` · `checkbox` · `table` (sortable headers, row hover, badge cells, empty state) ·
`pagination` · `tabs` (with counts) · `sidebar` + `sidebar-item` (active, count, collapsed rail) ·
`topbar` (brand, tenant switcher, search, theme toggle, gear, notifications, user menu) ·
`dropdown-menu` (labels, items, danger, separators) · `modal` · `slide-over` ·
`confirm-dialog` (htmx) · `toast` · `empty-state` (icon, title, text, action) ·
`timeline` (history) · `key-value` list · `inline-repeater` (channel/address rows) · `save-bar`
(sticky form footer).

### 7.4 Layout & Gentelella-derived patterns

- **Launcher** (`/t/<slug>/`): tablet-style home — a greeting + **compact app "infolets"** on a
  dense auto-fill grid (~4 per row) that **scales to many modules**. Each active tile shows a glyph,
  name, one-line description, and a metrics strip of up to three live counts (e.g. Contacts →
  People/Families/Birthdays). Modules not yet enabled render as muted **"coming soon"** tiles of the
  same footprint. Gear (Setup) in the top bar for Owners.
- **App shell:** sticky 57px **top bar** (brand · tenant switcher · global search · theme toggle ·
  gear (Owner) · notifications · user menu) + a **collapsible per-app sidebar** showing only that
  app's menu plus a small common section (⊞ All apps; Setup for Owners). Sidebar collapses to a
  68–70px **icon rail**. **Sidebar treatment = theme-matched (Option A)** — the selected standard.
- **Content canvas:** neutral background, `max-width: 1440px`, bordered white cards with soft
  layered shadows; **stat tiles** with tinted icon chips; **badge-heavy tables**.
- **Form density:** compact controls (34px height), 3-column form grids on wide screens collapsing
  to 2→1, sticky save bar. (Tuned during mockup review to fit more per screen.)

### 7.5 htmx & Alpine conventions

- **htmx:** partial templates for list filtering, inline edit, modal/slide-over swap targets, and
  the confirm-dialog. Server returns component-composed partials — never ad-hoc HTML.
- **Alpine:** presentation only (dropdown open/close, sidebar collapse, theme/palette toggle,
  tab switching). **No business logic in JS.**
- **`/styleguide`** (dev-only URL): renders every component, every variant, every state, in both
  themes and across palettes. It is the review surface for the UI gate.

---

## 8. Screens

Every screen composes components from §7.3 only. Mockups exist for the starred (★) screens.

**Auth (public):** login, accept-invite (new vs existing user), password-reset request/confirm,
tenant chooser — Gentelella-style centered auth cards.

**Launcher ★** — app tiles + counts + gear.

**Contacts app**
- **Dashboard ★** — stat tiles (People, Families, Birthdays in 30 days, Recently added), Upcoming
  birthdays & anniversaries (partial-date aware, days-away badges), Recently added / updated.
- **People list ★** — search, category **filter chips** with counts, sortable **table** (avatar,
  categories, primary contact, location, added), deceased badge, "no contact yet" empty cell,
  **pagination**.
- **Person detail ★** — identity header (avatar, name, preferred, category chips, marital badge,
  key facts) + **tabs** Overview / Relationships / Families / History. Overview: Details
  (key-value), Categories, Contact channels, Addresses, Relationships (P2P + P2O, gender-aware
  labels), Families, Important dates (partial-date), Notes.
- **Person create/edit ★** (`person-form`) — text/select fields, **partial-date widget**, deceased
  toggle, **inline repeatable** channel/address rows, category chooser, notes, validation **error
  state**, sticky **save bar**.
- **Families** — list; detail (members + a panel listing the relationships *between* members); form.
- **Relationship add/edit modal** — live person search + **gender-aware label preview**.

**Organizations app**
- **Dashboard** — counts by category + recents.
- **List** — search, category chips, table.
- **Detail** — Overview, Identifiers, Branches, Contact channels, **Key people** (P2O), History.
- **Forms** — org create/edit incl. branch management, identifiers, channels/addresses.

**Setup (Owner)** — Categories (Person & Org, system rows locked), Relationship types (P2P & P2O),
Members & invitations, **Appearance** (household palette; per-user theme), Recently deleted
(restore + gated hard delete), Tenant profile (name, logo).

**Sidebar A/B ★** — the treatment comparison (records the decision: A selected).

**Global:** 403, 404, 500 — on-brand empty/error pages composing `empty-state`.

---

## 9. Module framework (how module 2+ plugs in)

- **One Django app per module.** A module registers metadata on its `AppConfig` (a small registry):
  `name`, `icon` (Lucide id), `accent-tint` (for its glyph), `count_callable` (live launcher count),
  `url` (its dashboard). The launcher renders a compact infolet per registered module the tenant has
  enabled; modules known but not yet enabled render as muted "coming soon" tiles.
- **Per-app sidebar spec:** an ordered menu (label, icon, url, optional count) + the shared common
  section (All apps; Setup for Owners). Collapse behavior is inherited from the shell.
- **Dashboard pattern:** stat tiles + recents + upcoming, reusing the same components.
- **Consuming contacts:** modules reference `Person`/`Organization` by FK and rely on **system
  categories / relation types**. Example: a future **Banking** module lists Organizations filtered
  by the locked system category **"Bank"**, and links accounts to a Person via a P2O relationship.

---

## 10. Testing strategy

- **pytest-django** + **factory-boy** factories for every model.
- Per-feature **service** and **view** tests; forms tested incl. validation and error rendering.
- **Tenant-isolation gate (standing):** a reusable pattern creates two tenants, writes data in each,
  and asserts **zero cross-schema leakage** (queries in tenant A never see tenant B). Runs in CI on
  every phase; a phase cannot close if it fails.
- **PartialDate edge cases:** all missing-part combinations render correctly; upcoming-dates query
  handles null years and fully-unknown month/day.
- **Relationship label-resolution matrix:** for each seeded type × each gender pairing × each side,
  assert the rendered label (stored once, both sides render correctly).
- **Soft delete / restore / hard-delete gating** behavior per `ALLOW_HARD_DELETE`.

---

## 11. Docker / dev

`docker compose` services:
- **web** — Django `runserver`, source volume-mounted.
- **db** — PostgreSQL 17 with a named volume.
- **tailwind** — Tailwind CLI `--watch` compiling the stylesheet.
- **mailpit** — SMTP sink + web UI on `:8025` for invite/reset emails.

`.env` pattern (`.env.example` committed): `DATABASE_URL`, `SECRET_KEY`, `DJANGO_DEBUG`,
`ALLOW_HARD_DELETE` (`1` in dev), email settings pointing at Mailpit.
Task runner **`dev.ps1`** (+ `Makefile` parity): `up`, `down`, `migrate`, `makemigrations`,
`seed`, `createtenant`, `bootstrap`, `test`, `tailwind`, `shell`.

---

## 12. Non-goals for module 1

Deferred to later modules/phases: import/export, notifications, REST API, i18n, billing, global
cross-app search, reports/analytics. Build seams (module registry, system categories) so these slot
in without rework.
