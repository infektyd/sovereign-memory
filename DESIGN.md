---
version: alpha
name: Sovereign Memory Console
description: Agent-first operational interface for local memory, context preparation, and dry-run learning review.
colors:
  primary: "#151819"
  secondary: "#4F5A57"
  tertiary: "#2F7D68"
  accent: "#A95533"
  neutral: "#F4F1EA"
  surface: "#FFFFFF"
  surface-muted: "#E8E2D7"
  success: "#2F7D68"
  warning: "#B9852A"
  danger: "#A4483F"
  info: "#3D6F95"
  on-primary: "#F8F5EF"
  on-tertiary: "#FFFFFF"
typography:
  display:
    fontFamily: Inter
    fontSize: 34px
    fontWeight: 720
    lineHeight: 1.08
    letterSpacing: 0
  title:
    fontFamily: Inter
    fontSize: 20px
    fontWeight: 680
    lineHeight: 1.2
    letterSpacing: 0
  body:
    fontFamily: Inter
    fontSize: 15px
    fontWeight: 420
    lineHeight: 1.55
    letterSpacing: 0
  label:
    fontFamily: "IBM Plex Mono"
    fontSize: 12px
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: 0
rounded:
  sm: 4px
  md: 8px
  lg: 10px
spacing:
  xs: 4px
  sm: 8px
  md: 16px
  lg: 24px
  xl: 32px
components:
  button-primary:
    backgroundColor: "{colors.tertiary}"
    textColor: "{colors.on-tertiary}"
    rounded: "{rounded.md}"
    padding: 10px 14px
  panel:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.md}"
    padding: 16px
  app-shell:
    backgroundColor: "{colors.neutral}"
    textColor: "{colors.primary}"
    padding: 24px
  sidebar:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    padding: 24px
  metadata:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.secondary}"
    typography: "{typography.label}"
  separator:
    backgroundColor: "{colors.surface-muted}"
    textColor: "{colors.primary}"
    height: 1px
  status-safe:
    backgroundColor: "{colors.success}"
    textColor: "{colors.on-tertiary}"
  status-private:
    backgroundColor: "{colors.warning}"
    textColor: "{colors.primary}"
  status-danger:
    backgroundColor: "{colors.danger}"
    textColor: "{colors.on-tertiary}"
  status-info:
    backgroundColor: "{colors.info}"
    textColor: "{colors.on-tertiary}"
  risk-callout:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.on-tertiary}"
---

# Sovereign Memory Console

## Overview

The console is a quiet operational tool for agents and humans inspecting local memory behavior. It should feel precise, trustworthy, and private by default: more like a control room than a marketing page. The interface emphasizes evidence, ranking, privacy status, and next actions over decoration.

## Colors

- **Primary (#151819):** Near-black ink for important text and headers.
- **Secondary (#4F5A57):** Green-gray metadata and borders, calm enough for repeated use.
- **Tertiary (#2F7D68):** Sovereign green for primary action and successful local checks.
- **Accent (#A95533):** Warm clay for warnings, risk hints, and attention points.
- **Neutral (#F4F1EA):** Warm paper background that avoids clinical white.
- **Surface (#FFFFFF):** Main panels and dense reading areas.

## Typography

Use Inter for interface text and IBM Plex Mono for machine evidence: tool names, paths, scores, packet fields, timestamps, and status pills. Keep letter spacing at zero. Headings should be compact and utilitarian rather than oversized.

## Layout

Use a restrained dashboard layout with a persistent left rail, top status band, and dense grid panels. Prefer split panes, tables, and inspectors over hero sections or promotional cards. Fixed-format elements such as meters, status chips, and source rows should have stable dimensions.

## Elevation & Depth

Use borders and subtle shadows only to separate operational surfaces. Avoid floating page sections. Repeated packet/source/result items can use 8px-radius cards.

## Shapes

Use small radii: 4px for chips and inputs, 8px for buttons and panels. Avoid pill-heavy styling except for compact status badges where the shape helps scanning.

## Components

Primary controls use Sovereign green. Risk and privacy warnings use warm clay or warning ochre. Source rows must always show score, privacy level, authority, and inclusion reason. Outcome review surfaces must clearly separate learn candidates from log-only or do-not-store items.

## Do's and Don'ts

- Do show local/AFM/daemon status as evidence, not decoration.
- Do expose why a source was included and whether it is safe for AFM.
- Do make JSON packet inspection available without requiring a server.
- Do not auto-learn from frontend actions.
- Do not imply private vault or adapter material is safe to publish.
- Do not use large hero sections, gradients, or decorative background blobs.
