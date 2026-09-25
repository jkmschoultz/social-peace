"""Per-platform publishers. Each takes a rendered mp4 + its metadata sidecar and posts
it via that platform's official API.

Status:
  youtube   - live (OAuth + resumable upload)
  tiktok    - Content Posting API Direct Post; one-time `social-peace tiktok-login`
  instagram - live (Instagram Graph API, Reels, via a public URL / cloudflared tunnel)
"""
