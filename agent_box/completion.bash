# agent-box bash completion; load with: source <(agent-box completion bash)
# Candidates come from `agent-box _complete`, so this script never lists commands itself.

# Bash picks a completion by the typed word and never expands an alias for it, so the
# caller's `words` has an alias in words[0] replaced by its expansion, nested ones included;
# success means the words now start with agent-box, whose arguments follow.
_agent_box_unalias() {
    local depth
    local -a expansion
    for depth in 1 2 3 4 5 6 7 8; do
        [[ ${words[0]} == agent-box || ${words[0]} == */agent-box ]] && return 0
        [[ -n ${words[0]} && -n ${BASH_ALIASES[${words[0]}]+set} ]] || return 1
        # Quotes are dropped, not parsed: honouring them needs eval, which would run the alias's substitutions on Tab.
        read -ra expansion <<< "${BASH_ALIASES[${words[0]}]//[\"\']/}"
        ((${#expansion[@]})) || return 1
        words=("${expansion[@]}" "${words[@]:1}")
    done
    return 1
}

_agent_box() {
    local line=${COMP_LINE:0:COMP_POINT} mode word cur drop
    local -a words typed reply
    read -ra words <<< "$line"
    [[ -z $line || $line == *[[:space:]] ]] && words+=("")
    # A wrapper function or symlink registered by hand passes its arguments unchanged.
    typed=("${words[@]}")
    _agent_box_unalias || words=("${typed[@]}")
    # Readline replaces only the text between the last word break (= or :) and the cursor;
    # COMP_WORDS[COMP_CWORD] is not that text when the cursor is mid-word or right after a break.
    cur=${words[-1]##*[$COMP_WORDBREAKS]}
    { IFS= read -r mode; mapfile -t reply; } < <(command agent-box _complete "${words[@]:1}" 2>/dev/null)
    COMPREPLY=()
    case $mode in
        files) compopt -o filenames 2>/dev/null; mapfile -t COMPREPLY < <(compgen -f -- "$cur") ;;
        dirs) compopt -o filenames 2>/dev/null; mapfile -t COMPREPLY < <(compgen -d -- "$cur") ;;
        *)
            # Candidates are whole words, so they lose what precedes the replaced text.
            drop=$(( ${#words[-1]} - ${#cur} ))
            for word in "${reply[@]}"; do
                word=${word%%$'\t'*}
                COMPREPLY+=("${word:drop}")
            done
            ;;
    esac
}

complete -F _agent_box agent-box
# Aliases defined before this line complete like agent-box; a later one has to be registered by hand.
_agent_box_register() {
    local name
    local -a words
    for name in "${!BASH_ALIASES[@]}"; do
        words=("$name")
        _agent_box_unalias && complete -F _agent_box "$name"
    done
}
_agent_box_register
unset -f _agent_box_register
