#compdef agent-box
# agent-box zsh completion; load after compinit with: source <(agent-box completion zsh)
# Candidates come from `agent-box _complete`, so this script never lists commands itself.

# zsh expands an alias before completing unless complete_aliases is set; then the caller's
# `typed` has an alias in typed[1] replaced by its expansion, nested ones included;
# success means the words now start with agent-box or a path to it, whose arguments follow.
_agent_box_unalias() {
    local depth
    for depth in {1..8}; do
        [[ ${typed[1]} == agent-box || ${typed[1]} == */agent-box ]] && return 0
        [[ -n ${typed[1]} && -n ${aliases[${typed[1]}]+set} ]] || return 1
        typed=(${(Q)${(z)aliases[${typed[1]}]}} "${(@)typed[2,-1]}")
    done
    return 1
}

_agent_box() {
    local -a lines candidates typed
    local line word description
    # words keeps the quotes as typed, or as an alias zsh expanded itself spelled them.
    typed=("${(@Q)words[1,CURRENT]}")
    # A wrapper function or symlink registered by hand passes its arguments unchanged to agent-box.
    _agent_box_unalias || typed=(agent-box "${(@Q)words[2,CURRENT]}")
    [[ ${typed[1]} == '~/'* ]] && typed[1]=$HOME/${typed[1]#'~/'}
    lines=("${(@f)$(command ${typed[1]} _complete "${(@)typed[2,-1]}" 2>/dev/null)}")
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
        _agent_box_unalias && compdef _agent_box $name
    done
}
