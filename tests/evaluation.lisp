;;; Bounds and result contracts use the actual portable listener definitions.

(load (merge-pathnames "../lisp-mcp.lisp" *load-truename*))
(load (merge-pathnames "service-fixture.lisp" *load-truename*))
(in-package #:genera-protocol-fixture)

(defvar *eval-checks* 0)
(defun echeck (expected actual)
  (incf *eval-checks*)
  (unless (equal expected actual) (error "Expected ~S, got ~S" expected actual)))
(defun erejected (function)
  (echeck t (handler-case (progn (funcall function) nil) (error () t))))

(let* ((budget (make-mcp-budget :remaining 6))
       (capture (mcp-new-capture budget)))
  (dolist (code '(65 137 138 141 300 34))
    (mcp-capture-character capture (code-char code)))
  (echeck '(65 9 10 13 63 34) (map 'list #'char-code (mcp-capture-text capture)))
  (echeck t (mcp-budget-replaced budget))
  (echeck nil (mcp-budget-truncated budget))
  (dotimes (i 100000) (mcp-capture-character capture #\x))
  (echeck 6 (length (mcp-capture-text capture)))
  (echeck 0 (mcp-budget-remaining budget))
  (echeck t (mcp-budget-truncated budget)))

(let* ((budget (make-mcp-budget :remaining 2))
       (first (mcp-new-capture budget))
       (second (mcp-new-capture budget t)))
  (mcp-capture-character first #\a)
  (echeck nil
          (catch 'mcp-output-limit
            (mcp-capture-character second #\b)
            (mcp-capture-character second #\c)
            :not-bounded))
  (echeck "a" (mcp-capture-text first))
  (echeck "b" (mcp-capture-text second))
  (echeck t (mcp-budget-truncated budget)))

(let* ((request '(:version 1 :id "eval" :op :eval :boot-id "b" :service-id "s"
                 :form "(values)"))
       (reply '(:version 1 :id "eval" :ok t :boot-id "b" :service-id "s"
               :request-state :completed :values nil :value-count 0
               :stdout "" :stderr "" :warnings nil :package "USER"
               :elapsed-ms 0 :truncated nil :replaced nil)))
  (echeck reply (mcp-v1-check-eval-response request reply))
  (dolist (key '(:request-state :values :value-count :stdout :stderr :warnings
                 :package :elapsed-ms :truncated :replaced))
    (let ((incomplete (copy-list reply)))
      (remf incomplete key)
      (erejected (lambda () (mcp-v1-check-eval-response request incomplete)))))
  (dolist (key '(:id :boot-id :service-id :request-state :replaced))
    (let ((wrong (copy-list reply)))
      (setf (getf wrong key) "wrong")
      (erejected (lambda () (mcp-v1-check-eval-response request wrong)))))
  (let ((failure (copy-list reply)))
    (setf (getf failure :ok) nil (getf failure :request-state) :failed
          (getf failure :error) '(:code :reader-error :condition-type "READER-ERROR"
                                 :message "Bad form"))
    (echeck failure (mcp-v1-check-eval-response request failure))
    (setf (getf (getf failure :error) :condition-type) 3)
    (erejected (lambda () (mcp-v1-validate failure :response)))
    (remf (getf failure :error) :condition-type)
    (erejected (lambda () (mcp-v1-check-eval-response request failure))))
  (let ((budget (make-mcp-budget)) (bounded (copy-list reply)))
    (let ((capture (mcp-new-capture budget)))
      (dotimes (i (* 2 +mcp-output-limit+))
        (mcp-capture-character capture #\"))
      (setf (getf bounded :stdout) (mcp-capture-text capture)
            (getf bounded :truncated) (mcp-budget-truncated budget))
      (echeck t (< (length (mcp-v1-encode bounded :response)) +mcp-max-frame-length+)))))

(format t "~&Evaluation unit checks: ~D passed.~%" *eval-checks*)
