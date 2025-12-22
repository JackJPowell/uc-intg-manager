# Modal Component Usage Guide

## Installation

The release notes feature requires the `markdown` library:

```bash
pip install markdown
# or if using the requirements file
pip install -r requirements.txt
```

## Overview
The reusable modal component provides a dismissable overlay for displaying dynamic content. It's designed to work seamlessly with HTMX for loading content dynamically.

## Features
- Click outside to dismiss (backdrop click)
- ESC key to close
- Close X button in top right
- Smooth open/close animations
- Scroll lock when open
- Dynamic content loading via HTMX
- Customizable title and footer

## Basic Usage

### Method 1: JavaScript API

```javascript
// Open modal with title and content
openModal('Release Notes', '<p>Your content here</p>');

// Open modal with just title (shows loading spinner)
openModal('Loading...');

// Update content after loading (e.g., from HTMX response)
updateModalContent('<div>New content</div>');

// Update title
updateModalTitle('New Title');

// Close modal
closeModal();
```

### Method 2: HTMX Integration

Add these attributes to any clickable element:

```html
<!-- Simple click to open modal and load content -->
<button 
    onclick="openModal('Release Notes')"
    hx-get="/api/release-notes/owner/repo/v1.0.0"
    hx-target="#modal-content"
    hx-swap="innerHTML">
    View Release Notes
</button>
```

### Method 3: Make Version Links Open Modal

Update version links to open the modal instead of navigating:

```html
<!-- Example: Version link that opens release notes in modal -->
<a href="#" 
   onclick="event.preventDefault(); openModal('Release Notes - v{{ version }}'); return false;"
   hx-get="/api/release-notes/{{ owner }}/{{ repo }}/v{{ version }}"
   hx-target="#modal-content"
   hx-swap="innerHTML"
   class="hover:text-uc-primary transition-colors">
    v{{ version }}
</a>
```

## Example Routes to Create

### 1. Release Notes Route

```python
@app.route("/api/release-notes/<owner>/<repo>/<version>")
def get_release_notes(owner: str, repo: str, version: str):
    """Get release notes for a specific version and return HTML for modal."""
    if not _github_client:
        return "<p class='text-red-400'>GitHub client not available</p>"
    
    try:
        # Fetch release info from GitHub
        release = _github_client.get_release_by_tag(owner, repo, version)
        
        if not release:
            return "<p class='text-gray-400'>Release notes not found</p>"
        
        # Render the release notes template
        return render_template(
            "partials/modal_release_notes.html",
            version=version,
            release_date=release.get("published_at", ""),
            release_notes=release.get("body", ""),
            github_url=f"https://github.com/{owner}/{repo}/releases/tag/{version}"
        )
    except Exception as e:
        return f"<p class='text-red-400'>Error loading release notes: {e}</p>"
```

### 2. Version History Route

```python
@app.route("/api/version-history/<driver_id>")
def get_version_history(driver_id: str):
    """Get version history for an integration."""
    # Load from backups or GitHub releases
    versions = [...]  # Your logic here
    
    return render_template(
        "partials/modal_version_history.html",
        driver_id=driver_id,
        versions=versions
    )
```

## Advanced Features

### Show Footer

```javascript
// Add footer content (e.g., action buttons)
showModalFooter(`
    <div class="flex justify-end gap-2">
        <button onclick="closeModal()" class="px-4 py-2 bg-gray-600 hover:bg-gray-500 rounded-lg">
            Cancel
        </button>
        <button class="px-4 py-2 bg-uc-primary hover:bg-uc-secondary rounded-lg">
            Confirm
        </button>
    </div>
`);

// Hide footer
hideModalFooter();
```

### HTMX Event Handlers

```html
<!-- Update modal title after content loads -->
<button 
    hx-get="/api/content"
    hx-target="#modal-content"
    hx-on::after-request="updateModalTitle('Content Loaded')"
    onclick="openModal('Loading...')">
    Load Content
</button>
```

## CSS Classes Used

- `uc-card` - Background color
- `uc-border` - Border color
- `uc-darker` - Footer background
- `uc-primary` - Primary button color
- `uc-secondary` - Secondary/hover button color

## Accessibility

- Modal traps focus when open
- ESC key closes modal
- Click outside closes modal
- ARIA labels on close button
- Prevents body scroll when open
