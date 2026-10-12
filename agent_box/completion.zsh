#compdef agent-box
# agent-box zsh completion; load after compinit with: source <(agent-box completion zsh)
# Candidates come from `agent-box _complete`, so this script never lists commands itself.
# The agent-box that printed this script answers, so an alias spelling its path in any way completes alike.

# zsh expands an alias before completing unless complete_aliases is set; then the caller's
# `typed` has an alias in typed[1] replaced by its expansion as zsh would: nested ones too,
# but none twice, so `alias agent-box='agent-box --review off'` ends its chain.
# Success means the words now start with agent-box or a path to it, whose arguments follow.
_agent_box_unalias() {
    local -A seen
    while [[ -n ${typed[1]} && -n ${aliases[${typed[1]}]+set} && -z ${seen[${typed[1]}]} ]]; do
        seen[${typed[1]}]=1
        typed=(${(Q)${(z)aliases[${typed[1]}]}} "${(@)typed[2,-1]}")
    done
    [[ ${typed[1]} == agent-box || ${typed[1]} == */agent-box ]]
}

_agent_box() {
    local -a lines candidates typed
    local line word description
    # words keeps the quotes as typed, or as an alias zsh expanded itself spelled them.
    typed=("${(@Q)words[1,CURRENT]}")
    # Without complete_aliases zsh has expanded the alias already; expanding again would repeat it.
    # A wrapper function or symlink registered by hand passes its arguments unchanged.
    [[ -o complete_aliases ]] && { _agent_box_unalias || typed=("${(@Q)words[1,CURRENT]}") }
    lines=("${(@f)$(command @AGENT_BOX@ _complete "${(@)typed[2,-1]}" 2>/dev/null)}")
    case ${lines[1]} in
        files|dirs)
            [[ $PREFIX == --*=* ]] && compset -P '--[^=]#='
            if [[ ${lines[1]} == dirs ]]; then _files -/; else _files; fi
            ;;
        *)
            for line in "${(@)lines[2,-1]}"; do
                [[ -n $line ]] || continue
                word=${line%%$'\t'*}
                description=${line#"$word"}
                description=${description#$'\t'}
                candidates+=("${word//:/\\:}${description:+:$description}")
            done
            _describe -t candidates agent-box candidates
            ;;
    esac
}

compdef _agent_box agent-box
# With complete_aliases, aliases defined before this line complete like agent-box; a later one has to be registered by hand.
() {
    local name
    local -a typed
    for name in ${(k)aliases}; do
        typed=($name)
        if _agent_box_unalias; then compdef _agent_box $name; fi
    done
}
