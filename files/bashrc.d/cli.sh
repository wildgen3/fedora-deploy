# Modern CLI integrations (each only if installed).
command -v zoxide >/dev/null && eval "$(zoxide init bash)"
command -v direnv >/dev/null && eval "$(direnv hook bash)"
[ -f /usr/share/fzf/shell/key-bindings.bash ] && . /usr/share/fzf/shell/key-bindings.bash
# atuin needs bash-preexec (not packaged in Fedora; deploy-user.sh fetches it from upstream).
if command -v atuin >/dev/null && [ -f "$HOME/.local/share/bash-preexec.sh" ]; then
  . "$HOME/.local/share/bash-preexec.sh"
  eval "$(atuin init bash --disable-up-arrow)"
fi
command -v eza >/dev/null && alias ll='eza -l --git --group-directories-first'
