# Skill: Copy File Between Directories

## Procedure

1. **Search for source file**
   ```bash
   search_files(path="~/Documents", pattern="*Lumina Codebase Metrics Overview*")
   ```
   - Use `path` to specify root directory (use "~" for home)
   - Use `pattern` with glob syntax (`*name*`, `*.py`) or specific filename
   - If searching by content: add `content="keyword"` parameter

2. **Extract file path**
   ```python
   # Parse search result (first item is the match)
   source_path = results[0]['path']  # e.g., '/home/bino/Documents/file.txt'
   dest_path = '~/lumina/handoffs/' + filename
   ```

3. **Read content** (if binary/media, skip or handle separately)
   ```python
   read_file(path=source_path)  # returns {'file': ..., 'bytes': ...}
   ```
   - Check file size first: files > 1MB might indicate media/binary
   - For very large files (>50KB), consider `cp` command instead

4. **Write to destination**
   ```python
   write_file(path=dest_path, content=file_content)  # mode defaults to overwrite
   ```
   - Use `mode="append"` if you need to preserve existing content
   - Verify write succeeded: check return value and bytes written

5. **Verify** (optional but recommended)
   ```python
   read_file(path=dest_path)  # confirm content matches
   ```

## Pitfalls

- **Hidden files**: `search_files` doesn't show hidden files by default. Add `show_hidden=True` to list_dir if needed, but search_files may miss them.
- **Permissions**: If destination directory requires elevated permissions, the write will fail.
- **Symlinks**: If source is a symlink and you read content, you get symlink target's content (not the link itself).
- **Large binary files**: Don't use `read_file` for images, videos, or large databases. Use shell commands (`cp`, `dd`) instead.
- **Truncated writes**: If write fails mid-process, file may be corrupted. Always verify after writing.

## Verification

```bash
# Compare checksums (if available)
cp -l source dest  # preserves permissions and timestamps
ls -lh ~/lumina/handoffs/  # verify size matches
```

**Success criteria**: Destination file exists with matching content, same permissions if possible.