"""UI-only fifth-tab revision; reuse the preserved consolidation server."""
import run_consolidation_review as server

if __name__ == "__main__":
    server.BASE_SLUG = server.SLUG
    server.SLUG = "consolidation_review_20260906_v2"
    server.main()
