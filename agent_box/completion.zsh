#compdef agent-box
# agent-box zsh completion; load after compinit with: source <(agent-box completion zsh)
# Candidates come from `agent-box _complete`, so this script never lists commands itself.
_agent_box() {
    local -a lines candidates
    local line word description
    lines=("${(@f)$(command agent-box _complete "${(@)words[2,CURRENT]}" 2>/dev/null)}")
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
