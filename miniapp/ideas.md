# TgFox Mini App — Reference-Led UI Specification

## Ground-Truth Reference

The supplied CasherCard screenshots are the visual and UX benchmark. TgFox must adopt their **clean white dashboard shell**, airy blue-accent utility cards, compact top header, slide-out left menu, short operational labels, and transaction-table clarity. The supplied **TG FOX API circular wolf logo** is the official brand mark; it replaces all generated fox symbols and wordmarks.

## Chosen Direction: TgFox Blue Console

### Design Movement

Clean, modern fintech console inspired by the reference dashboard: white pages, pale blue information panels, electric blue action buttons, thin gray dividers, and small practical typography. It should feel like a trusted account-control panel inside Telegram, not an editorial marketplace or a dark crypto terminal.

### Core Principles

1. **Utility before decoration.** Every panel explains a balance, a transaction state, or a direct user action.
2. **Reference-level clarity.** Controls are short, consistently placed, and presented in an uncomplicated card grid.
3. **Blue indicates action.** Bright TgFox blue is reserved for action buttons, selected navigation, and active data states.
4. **Logo-led trust.** The supplied logo is visible in the header and menu; no substitute brand mark is used.

### Color Philosophy

White and soft cloud-gray surfaces keep the workspace light. TgFox blue (`#1769F5`) handles primary actions and selected states; a softer sky blue supports informative cards. Mint means confirmed/available, amber means pending, and rose remains reserved for blockers.

### Layout Paradigm

A fixed compact top bar anchors the logo, menu and profile status. The dashboard uses a responsive two-column utility grid on wide screens and a single vertical stack inside Telegram. The menu is a left-side drawer matching the reference navigation hierarchy. Long-form activity becomes a simple table-like list rather than a decorative feed.

### Signature Elements

1. The official **TG FOX API wolf logo** in the white header and menu profile block.
2. Rounded white utility cards with pale-blue action zones and horizontal blue CTA buttons.
3. A minimal account-summary bar chart and circular inventory indicator for at-a-glance status.

### Interaction Philosophy

Menu navigation opens from the top-left, primary action cards move the user into the relevant workspace, and major financial actions require a clear confirmation panel. Feedback stays lightweight and Telegram haptics are used only on decisive actions.

### Animation

The drawer slides from the left in 220ms. Cards use only a 120ms press response. Tab changes are instant with a 160ms opacity transition. All nonessential motion respects reduced-motion preferences.

### Typography System

**DM Sans** is used for all operational UI. Headlines use a 700 weight rather than a display font, keeping the app close to the clear reference dashboard. Values use tabular numeric settings.

### Brand Essence

**TgFox is a clear, fast Telegram account-control workspace for buying numbers, managing balance, and tracking orders.**

Personality: **reliable, fast, practical**.

### Brand Voice

Headlines are plain operational directions; buttons use direct verbs. Examples: “Balance top up” and “Review your latest orders.”

### Wordmark & Logo

Use the supplied TG FOX API circular wolf emblem as-is. Do not generate, redraw, recolor, or replace it.

### Signature Brand Color

**TgFox Action Blue — `#1769F5`**.

## Style Decisions

- TgFox Action Blue is a **solid** primary-action and selected-state color only; gradients, blue glows, and decorative halos are excluded.
- The first viewport behaves as an account-control console: session status, wallet balance, inventory, orders, and direct actions are compact utility modules rather than promotional cards.
- Labels use short operational language: “Wallet balance”, “Buy numbers”, “Recent orders”, and “Session verified”.
