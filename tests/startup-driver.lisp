;;; Test-only OS snapshot. Production launch, locks, processes, and RPC remain real.
(load (merge-pathnames "../lisp-mcp.lisp" *load-truename*))
(in-package #:lisp-mcp)
(genera-load-launch-module)
(let ((inspect (symbol-function 'genera-local-facts)))
  (setf (symbol-function 'genera-local-facts)
        (lambda (runtime)
          (let ((facts (funcall inspect runtime)))
            (setf (getf facts :wsl) t (getf facts :display) ":fixture"
                  (getf facts :display-socket) '(:kind :socket :write t)
                  (getf facts :tun) '(:kind :character :read t :write t)
                  (getf facts :tap-flags) #x1802 (getf facts :link-flags) #x1003
                  (getf facts :tap-owner) (getf facts :uid) (getf facts :tap-group) -1)
            facts))))
